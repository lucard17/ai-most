"""Confirmation of a dangerous action by a button of the owner in chat (spec: authority).

A post of the owner in chat is the owner's word, but before a deploy, a merge, an apply or a restart the intent must be
visible *before* the action. ``aimost confirm --spec <action.json>`` posts the action described by fields
(``confirm_spec``: what, why, steps, what stays, rollback, merge requests) with ✅/❌ from the role's bot and waits; an
accepted press writes a record, and the guard lets one command through with the prefix ``AIMOST_CONFIRM=<id>``. A merge
is covered only for the merge requests the description names in ``merges``.

    <marks>/<role>/<id>.json   {id, action, details, by, at (unix s), used, used_at, mrs, merges, commands}

Merge request cards in the post (title, state, CI) come from plugins (``CARDS``: fn(ref, conn) -> card or None); without
one the card says "not checked".

Patterns (extended regular expressions, case-insensitive, one per line, ``#`` — comment), user file **plus** built-in
defaults — a user file adds patterns, it cannot drop a default:

    <config>/confirm-patterns   in a chat-started turn only with a fresh unused confirmation
    <config>/confirm-deny       in a chat-started turn never (only by the owner's word in the session itself)

Who may press: the owner, the role's co-owners and confirmers (``connections.confirmers``). Buttons are handled by the
edge: a press writes ``props.aimost_confirm`` = {id, answer yes|no, by, user_id, at} into the post; this module polls the
post (outbound connections only).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import connections, inbox, mattermost, paths, questions
from . import names
from .names import P

TTL_S = 900
ID_RE = re.compile(r"^[0-9a-f]{8}$")
# the command prefix that spends a confirmation: AIMOST_CONFIRM=<id>; on a server with another props prefix (D17) also
# <PREFIX>_CONFIRM=<id> — the spelling the earlier version taught its roles
_VARS = ["AIMOST"] + ([names.PREFIX.upper()] if names.PREFIX != "aimost" else [])
PREFIX_RE = re.compile(r"(?:" + "|".join(_VARS) + r")_CONFIRM=([0-9a-f]{8})")
LEADING_RE = re.compile(r"^(?:" + "|".join(_VARS) + r")_CONFIRM=([0-9a-f]{8})\b")

DEFAULT_CONFIRM = [
    r"\bterraform\s+apply\b",
    r"\btofu\s+apply\b",
    r"\bansible-playbook\b",
    r"\bkubectl\s+(apply|create|delete|replace|patch|scale|rollout|drain|cordon)\b",
    r"\bhelm\s+(install|upgrade|uninstall|rollback|delete)\b",
    r"\bglab\s+mr\s+merge\b",
    r"\bgh\s+pr\s+merge\b",
    r"\bssh\b.*\b(systemctl|service)\s+(restart|stop|start|reload)\b",
    r"\bssh\b.*\bdocker(\s+compose)?\s+(restart|stop|start|up|down|rm|kill)\b",
    r"/api/v2/silences|amtool\s+silence\s+(add|expire|update)",
]
DEFAULT_DENY = [
    r"\b(psql|mysql|mariadb|clickhouse-client|mongosh)\b.*\b(insert|update|delete|alter|drop|truncate|create|grant|revoke)\b",
]

# Merge request cards: fn(ref "repo!N", conn) -> {ref, url, title, state: open|draft|merged|closed, sha, ci} or None
# (not this plugin's reference). The first card wins; an exception counts as "not checked".
CARDS: List[Callable[[str, dict], Optional[dict]]] = []
MERGE_CMD = re.compile(r"\bglab\s+mr\s+merge\b|\bgh\s+pr\s+merge\b", re.I)

# Other ways an action counts as confirmed (plugins: e.g. a "merge?" question answered by the owner). fn(role, command,
# hook input) -> a reason string to let the command through, or None to abstain. Checked after deny, before the prefix.
GRANTS: List[Callable[[str, str, dict], Optional[str]]] = []


# ── patterns ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def _file_patterns(path: Path) -> List[str]:
    try:
        return [x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip() and not x.lstrip().startswith("#")]
    except OSError:
        return []


def patterns(kind: str) -> List[str]:
    """``kind`` is ``patterns`` or ``deny``: the user's file, then the built-in defaults."""
    defaults = DEFAULT_DENY if kind == "deny" else DEFAULT_CONFIRM
    user = _file_patterns(paths.config_dir() / f"confirm-{kind}")
    return user + [p for p in defaults if p not in user]


def match(command: str, pats: Sequence[str]) -> Optional[str]:
    """The first pattern the command matches. A broken user pattern is skipped, not fatal."""
    for p in pats:
        try:
            if re.search(p, command, re.I):
                return p
        except re.error:
            continue
    return None


# ── records ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def record_path(role: str, cid: str) -> Path:
    return paths.marks_dir() / inbox.valid_name(role) / f"{cid}.json"


def save(role: str, rec: dict) -> Path:
    d = paths.private_dir(paths.marks_dir() / inbox.valid_name(role))
    paths.private_dir(paths.marks_dir())
    p = d / f"{rec['id']}.json"
    tmp = d / f".{rec['id']}.tmp"
    tmp.write_text(json.dumps(rec, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return p


def peek(role: str, cid: str) -> Optional[dict]:
    """The record of a confirmation, unchanged (None — none such)."""
    if not ID_RE.match(cid or ""):
        return None
    try:
        return json.loads(record_path(role, cid).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None


def merge_ref(command: str, cwd: str = "") -> Optional[str]:
    """Which merge request a merge command merges: ``repo!N`` for ``glab mr merge N``, ``repo#N`` for ``gh pr merge N``;
    the repository from ``-R/--repo``, else from the ``origin`` of the directory it runs in (``cd <dir> && …`` or the
    hook's cwd). None — cannot tell (no number, several, unknown repository)."""
    import shlex
    import subprocess
    seg = next((x for x in re.split(r"&&|;|\|\|", command) if MERGE_CMD.search(x)), "")
    if not seg:
        return None
    try:
        w = shlex.split(seg)
    except ValueError:
        w = seg.split()
    gl = MERGE_CMD.search(seg).group(0).lower().startswith("glab")
    noun = "mr" if gl else "pr"
    i = next((k for k in range(len(w) - 1) if w[k] == noun and w[k + 1] == "merge"), None)
    nums = [t for t in w[i + 2:] if re.fullmatch(r"\d+", t)] if i is not None else []
    repo = next((w[k + 1] for k, t in enumerate(w[:-1]) if t in ("--repo", "-R")), None) \
        or next((t.split("=", 1)[1] for t in w if t.startswith("--repo=")), None)
    if not repo:
        m = re.search(r"\bcd\s+(\S+)", command)
        d = os.path.expanduser(m.group(1)) if m else (cwd or ".")
        if not os.path.isabs(d):
            d = os.path.join(cwd or ".", d)
        try:
            url = subprocess.run(["git", "-C", d, "remote", "get-url", "origin"], capture_output=True, text=True,
                                 timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            url = ""
        m = re.search(r"[:/]([\w.-]+/[\w.-]+?)(?:\.git)?\s*$", url)
        repo = m.group(1) if m else None
    if not repo or len(nums) != 1:
        return None
    return f"{repo.rstrip('/').rsplit('/', 1)[-1]}{'!' if gl else '#'}{nums[0]}"


def spend(role: str, cid: str, now: Optional[float] = None) -> Optional[str]:
    """Use confirmation ``cid`` of the role once. None — spent now (the command may run); otherwise why not:
    ``missing`` or ``used or older than 15 minutes``."""
    if not ID_RE.match(cid or ""):
        return "missing"
    p = record_path(role, cid)
    now = time.time() if now is None else now
    try:
        f = open(p, "r+", encoding="utf-8")
    except OSError:
        return "missing"
    with f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            rec = json.loads(f.read() or "{}")
        except ValueError:
            return "missing"
        if rec.get("used") is not False or now - float(rec.get("at") or 0) > TTL_S:
            return "used or older than 15 minutes"
        rec["used"], rec["used_at"] = True, now
        f.seek(0)
        f.truncate()
        f.write(json.dumps(rec, ensure_ascii=False))
    return None


# ── the post ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def render_confirm(cid: str, action: str, details: str, agent: Optional[str], url: str, auth: Dict[str, str],
                   done: Optional[dict] = None, to: Optional[List[str]] = None) -> Tuple[str, list]:
    """Message and attachments of a confirmation post: ✅/❌ buttons, or the outcome once pressed. Shared with the edge,
    which re-renders the post on a press."""
    head = f"🛡 **Confirm?** {action}" + (f"\n{details}" if details else "")
    if done:
        verdict = "✅ confirmed" if done.get("answer") == "yes" else "❌ declined"
        return f"{head}\n\n{verdict} — @{done.get('by')}", []
    ctx = {"kind": "confirm", "id": cid, "action": action, "details": details, "agent": agent, **auth}
    if to:
        ctx["to"] = list(to)
    return head + "\n_One “yes” — one action, 15 minutes._", [{"actions": [
        {"id": "yes", "name": "✅ Confirm", "type": "button", "style": "good",
         "integration": {"url": url, "context": dict(ctx, answer="yes")}},
        {"id": "no", "name": "❌ Cancel", "type": "button", "style": "danger",
         "integration": {"url": url, "context": dict(ctx, answer="no")}}]}]


TEMPLATE = ("🛡 **Confirm: {do}**\n{why}\n{cards}\n**What happens:**\n{steps}\n\n**Stays the same:** {keeps}\n"
            "**Rollback:** {rollback}\n_{rule}_")
CARD = "- **MR:** [{ref}]({url}) «{title}» · {state} · CI on `{sha}` — {ci}"
CARD_UNKNOWN = "- **MR:** {ref} — not checked (no answer about it)"
RULE = "One “yes” — one action, 15 minutes."
DONE = {"yes": "✅ confirmed", "no": "❌ declined"}


def rule(merges: Sequence[str]) -> str:
    """The line under the buttons: a merge is promised only for the merge requests in ``merges`` — not for numbers found
    in the text."""
    return f"Merge of {', '.join(merges)} — once each; anything else — one “yes”, one action; 15 minutes." if merges else RULE


def card_line(c: dict) -> str:
    if c.get("title") is None:
        return CARD_UNKNOWN.format(ref=c["ref"])
    return CARD.format(ref=c["ref"], url=c.get("url") or "", title=c["title"], state=c.get("state") or "?",
                       sha=c.get("sha") or "?", ci=c.get("ci") or "no pipeline")


def render_confirm_spec(cid: str, spec: dict, cards: Sequence[dict], agent: Optional[str], url: str,
                        auth: Dict[str, str], to: Optional[List[str]] = None) -> Tuple[str, list]:
    """A confirmation post from a described action: the text by ``TEMPLATE`` and ✅/❌. On a press the edge keeps the
    text the presser saw and adds the outcome (``confirm_done``) — it does not rebuild the post from fields."""
    lines = [card_line(c) for c in cards]
    msg = TEMPLATE.format(do=spec["do"], why=spec["why"], cards=("\n" + "\n".join(lines) + "\n") if lines else "",
                          steps="\n".join(f"{i}. {x}" for i, x in enumerate(spec["steps"], 1)), keeps=spec["keeps"],
                          rollback=spec["rollback"], rule=rule(spec.get("merges") or []))
    ctx = {"kind": "confirm", "v": 2, "id": cid, "agent": agent, **auth}
    if to:
        ctx["to"] = list(to)
    return msg, [{"actions": [
        {"id": "yes", "name": "✅ Confirm", "type": "button", "style": "good",
         "integration": {"url": url, "context": dict(ctx, answer="yes")}},
        {"id": "no", "name": "❌ Cancel", "type": "button", "style": "danger",
         "integration": {"url": url, "context": dict(ctx, answer="no")}}]}]


def without_rule(message: str) -> str:
    """The post's text without its last line when that is the italic rule line."""
    lines = (message or "").rstrip().split("\n")
    if len(lines) > 1 and lines[-1].startswith("_") and lines[-1].endswith("_"):
        lines = lines[:-1]
    return "\n".join(lines).rstrip()


def confirm_done(message: str, done: dict) -> str:
    """The post after a press: the text the presser saw, without the rule line, plus the outcome."""
    return without_rule(message) + f"\n\n{DONE.get(done.get('answer'), DONE['no'])} — @{done.get('by')}"


def cards_for(spec: dict, conn: dict) -> List[dict]:
    """Cards of the description's merge requests; a merge request in ``merges`` that is already merged or closed — a
    ConfirmError (code 4): there is nothing to merge."""
    out = []
    for ref in spec.get("mrs") or []:
        card = None
        for fn in CARDS:
            try:
                card = fn(ref, conn)
            except Exception:
                card = None
            if card:
                break
        card = dict(card or {"title": None}, ref=ref)
        if ref in (spec.get("merges") or []) and card.get("state") in ("merged", "closed"):
            raise ConfirmError(f"{ref} is already {card['state']} — nothing to merge (merges)", 4)
        out.append(card)
    return out


class ConfirmError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def request(role: str, spec: dict, via: str = "", channel: str = "", thread: str = "",
            to: Sequence[str] = (), timeout: float = TTL_S, log: Callable[[str], None] = lambda m: None) -> str:
    """Post a confirmation of a described action (``confirm_spec.check`` passed) and wait for an allowed press. Returns
    the confirmation id on ✅; raises ConfirmError with code 1 on ❌, 2 on timeout or ``to`` outside the allowed list (no
    post), 3 when the role may not post, 4 when a merge request in ``merges`` is already merged or closed (no post)."""
    action, details = spec["do"], "; ".join(spec["steps"])
    conns = connections.connections()
    conn = conns.get(via) if via else connections.own_connection(role)
    if not conn or not connections.admitted(conn, role):
        raise ConfirmError(f"session {role} is not admitted by a connection — cannot confirm in chat", 3)
    token = connections.bot(conn, role)
    if not token:
        raise ConfirmError(f"{role} has no bot in {conn['NAME']}", 3)

    def api(method: str, path: str, body: object = None):
        return mattermost.api(conn, method, path, body, token=token)

    names = connections.confirmers(conn, role)
    to = [x.strip().lstrip("@") for x in to if x.strip()]
    bad = [x for x in to if x not in names]
    if bad:
        raise ConfirmError(f"--to {', '.join('@' + x for x in bad)}: a confirmation of {role} is pressed only by "
                           + ", ".join("@" + n for n in names) + " (OWNER, CO_OWNERS, CONFIRMERS) — another press would not count", 2)
    me = api("GET", "/users/me")["id"]
    ids = {n: api("GET", f"/users/username/{n}")["id"] for n in names}
    allowed = set(ids.values())
    owner = conn["OWNER"]
    if thread and not channel:
        channel_id = api("GET", f"/posts/{thread}")["channel_id"]
    elif (channel or "dm") == "dm":
        channel_id = api("POST", "/channels/direct", [me, ids[owner]])["id"]
    else:
        channel_id = api("GET", f"/teams/name/{conn['TEAM']}/channels/name/{channel}")["id"]
    cards = cards_for(spec, conn)
    cid = secrets.token_hex(4)
    msg, atts = render_confirm_spec(cid, spec, cards, role, questions.action_url(conn), questions.client_auth(conn),
                                    to=names)
    # Outside a DM the first line mentions who can press: the owner and the allowed people who wrote in the thread
    # (or --to instead) — otherwise nobody sees the confirmation and it expires.
    if (api("GET", f"/channels/{channel_id}") or {}).get("type") != "D":
        tag = [owner] + [x for x in to if x != owner]
        if not to and thread:
            wrote = {p.get("user_id") for p in (api("GET", f"/posts/{thread}/thread").get("posts") or {}).values()}
            tag += [n for n in names[1:] if ids[n] in wrote]
        msg = " ".join("@" + n for n in tag) + "\n" + msg
    post = {"channel_id": channel_id, "message": msg,
            "props": {"agent": role, "attachments": atts, P("confirm_request"): cid}}
    if thread:
        post["root_id"] = thread
    pid = api("POST", "/posts", post)["id"]
    log(f"waiting for ✅ (id {cid}, post {pid})")
    every = float(os.environ.get("AIMOST_CONFIRM_EVERY_S", "3"))
    deadline = time.time() + timeout
    while time.time() < deadline:
        done = ((api("GET", f"/posts/{pid}") or {}).get("props") or {}).get(P("confirm"))
        if isinstance(done, dict) and done.get("id") == cid and done.get("user_id") in allowed:
            if done.get("answer") != "yes":
                raise ConfirmError("❌ declined", 1)
            save(role, {"id": cid, "action": action, "details": details, "by": done.get("by"), "at": time.time(),
                        "used": False, "mrs": spec.get("mrs") or [], "merges": spec.get("merges") or [],
                        "commands": spec.get("commands") or []})
            return cid
        time.sleep(every)
    api("PUT", f"/posts/{pid}/patch", {"message": without_rule(msg) + "\n\n⌛ expired without an answer",
                                       "props": {"agent": role, "attachments": [], P("confirm_request"): cid}})
    raise ConfirmError("⌛ expired", 2)
