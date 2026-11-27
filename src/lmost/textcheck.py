"""Checks of outgoing text (spec: text; extensions → text checks form a pipeline).

Everything ``lmost post`` (and ``lmost channel``) sends goes through one pipeline before it reaches the chat:

1. built-in checks, in this order:
   - ``questions_as_text`` — two or more questions as text: code 4 (``--plain`` skips it);
   - ``private_guard``     — on a connection with ``PRIVATE_GUARD=on``: a private pattern matched, code 5;
   - ``logins``            — a ``@login`` or ``--to`` addressee missing on the server: code 6; a bot as addressee of
     buttons: code 6; a mentioned person or addressee not in the channel: code 7 (``--invite`` adds them);
2. enabled plugins from ``TEXT_CHECKS`` (exit codes 20–39), e.g. turning tracker keys or MR references into links.

A check is ``fn(draft) -> None``: it MAY rewrite the draft (``draft.map_text``), or raise ``Refused(code, hint)``. A check
that cannot reach its backend lets the text through (a plugin raising anything but ``Refused`` is reported on stderr and
skipped). The first refusal stops the post.

Also here: how a ``.md`` document becomes post text (``md_chunks``) — Mattermost renders markdown in posts, but shows a
``.md`` attachment raw, and phones do not open it at all.
"""

from __future__ import annotations

import re
import sys
from typing import Callable, Dict, List, Optional

from . import paths

# Plugin checks: fn(draft) -> None; refuse with Refused(code in 20..39, hint).
TEXT_CHECKS: List[Callable[["Draft"], None]] = []
PLUGIN_CODES = range(20, 40)


class Refused(Exception):
    def __init__(self, code: int, hint: str):
        super().__init__(hint)
        self.code = code
        self.hint = hint


class Draft:
    """What is about to go to chat. ``message`` — post text (or the new text of an edit, a pin, a channel header);
    ``chunks`` — further parts of a long document; ``ask`` — question spec (context, parts' titles and texts);
    ``files`` — attachment paths (names and text content are checked by the private guard); ``to`` — addressees of
    buttons. ``call(method, path, body)`` — server API as the role's bot (None — offline: server checks are skipped);
    ``channel_id`` / ``me`` — where it goes and who posts; ``invite`` — add non-members instead of refusing."""

    def __init__(self, message: str = "", *, chunks: Optional[List[str]] = None, ask: Optional[dict] = None,
                 files: Optional[List[str]] = None, to: Optional[List[str]] = None, conn: Optional[dict] = None,
                 plain: bool = False, call: Optional[Callable] = None, channel_id: str = "", me: str = "",
                 invite: bool = False, extra: Optional[List[str]] = None):
        self.message = message or ""
        self.chunks = list(chunks or [])
        self.ask = ask
        self.files = list(files or [])
        self.to = list(to or [])
        self.conn = conn or {}
        self.plain = plain
        self.call = call
        self.channel_id = channel_id
        self.me = me
        self.invite = invite
        self.extra = list(extra or [])     # other outgoing text (channel purpose, …) — checked, not rewritten
        self.notes: List[str] = []         # warnings for stderr (a check that could not reach its backend)

    def texts(self) -> List[str]:
        out = [self.message] + self.chunks + self.extra
        if self.ask is not None:
            out.append(self.ask.get("context") or "")
            for p in self.ask.get("parts") or []:
                out.append((p.get("title") or "") + "\n" + (p.get("text") or ""))
        return out

    def map_text(self, fn: Callable[[str], str]) -> None:
        """Rewrite every outgoing text with fn (message, chunks, the question's context and its parts)."""
        self.message = fn(self.message) if self.message else self.message
        self.chunks = [fn(c) for c in self.chunks]
        if self.ask is not None:
            if self.ask.get("context"):
                self.ask["context"] = fn(self.ask["context"])
            for p in self.ask.get("parts") or []:
                for k in ("title", "text"):
                    if p.get(k):
                        p[k] = fn(p[k])


def _strip_code(text: str) -> str:
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    return re.sub(r"`[^`\n]*`", " ", t)


# ── questions as text (code 4) ──────────────────────────────────────────────────────────────────────────────────────
def count_questions(text: str) -> int:
    """Questions to people in text: "?" ending a sentence, outside code and links."""
    t = re.sub(r"https?://\S+", " ", _strip_code(text))
    return len(re.findall(r"\?(?=\s|$|[)»*_])", t))


def questions_as_text(d: Draft) -> None:
    if d.plain or d.ask is not None:
        return
    n = count_questions(d.message)
    if n >= 2:
        raise Refused(4, f"{n} questions as text — ask people with buttons: the report as one post, the questions as\n"
                         "  lmost post --thread <thread> --ask \"<question>\" \"<option 1>\" \"<option 2>\" [--rec 1]"
                         "   (several — a .json spec)\nNot questions to people (a quote, a log) — --plain.")


# ── the owner's private matters (code 5) ───────────────────────────────────────────────────────────────────────────
def private_patterns() -> List[str]:
    """Patterns of ``<config>/private-patterns`` (one regex per line, ``#`` — comment). Missing file — OSError."""
    with open(paths.config_dir() / "private-patterns", encoding="utf-8") as f:
        return [x.strip() for x in f if x.strip() and not x.lstrip().startswith("#")]


def private_guard(d: Draft) -> None:
    if d.conn.get("PRIVATE_GUARD") != "on":
        return
    try:
        pats = private_patterns()
    except OSError:
        raise Refused(5, f"no {paths.config_dir() / 'private-patterns'} while the connection has PRIVATE_GUARD=on — "
                         "nothing posted")
    parts = d.texts()
    for f in d.files:
        parts.append(f.rsplit("/", 1)[-1])
        try:
            with open(f, "rb") as fh:
                raw = fh.read(4 << 20)
            if b"\0" not in raw[:8192]:            # text, not an image
                parts.append(raw.decode("utf-8", errors="replace"))
        except OSError:
            pass
    blob = "\n".join(parts)
    hit = [p for p in pats if re.search(p, blob, re.I)]
    if hit:
        raise Refused(5, f"private matters of the owner ({', '.join(hit)}) — a work chat gets only what concerns the "
                         "work; rephrase without it")


# ── logins and members (codes 6, 7) ────────────────────────────────────────────────────────────────────────────────
MENTION = re.compile(r"(?<![\w/@.`-])@([a-z0-9][a-z0-9._-]*[a-z0-9]|[a-z0-9])", re.I)
SPECIAL = {"here", "channel", "all"}


def mentions(text: str) -> set:
    """``@logins`` in text, lower-case; mentions inside code (``@types/node``) do not count."""
    return {m.group(1).lower() for m in MENTION.finditer(_strip_code(text))} - SPECIAL


def mentioned(text: str, login: str) -> bool:
    """Is ``@login`` in text — at the login's boundary, not as a substring (``@ann-bot`` does not mention ``@ann``)."""
    return bool(re.search(r"(?<![\w.@-])@" + re.escape(login.lower()) + r"(?![\w-]|\.\w)", (text or "").lower()))


def _warn(d: Draft, msg: str) -> None:
    d.notes.append(msg)


def logins(d: Draft) -> None:
    if d.call is None:
        return
    names = set().union(*[mentions(t) for t in d.texts()]) | {x.lower().lstrip("@") for x in d.to if x}
    names = sorted(n for n in names if n)
    if not names:
        return
    try:
        got = d.call("POST", "/users/usernames", names)
    except Exception as x:                   # the server did not answer the check — do not hold the post
        _warn(d, f"logins not checked ({str(x)[:120]})")
        return
    if not isinstance(got, list):
        return
    found = {u.get("username", "").lower(): u for u in got}
    missing = [n for n in names if n not in found]
    if missing:
        hints = []
        for n in missing:
            cand: List[str] = []
            for term in dict.fromkeys([n, re.split(r"[-_.]", n)[0], n.split("-")[-1]]):
                try:
                    cand += [u["username"] for u in (d.call("POST", "/users/search",
                                                            {"term": term, "allow_inactive": False}) or [])][:5]
                except Exception:
                    pass
            cand = list(dict.fromkeys(c for c in cand if c.lower() != n))[:4]
            hints.append(f"@{n} — not in this chat" + (f" (similar: {', '.join('@' + c for c in cand)})" if cand else ""))
        raise Refused(6, "unknown logins: " + "; ".join(hints) + ". Find a person's chat login — `lmost who <name>`.")
    _humans_only(d, [found[x.lower().lstrip("@")] for x in d.to if x])
    _members(d, {n: found[n].get("id") for n in names if found[n].get("id")})


def _humans_only(d: Draft, users: List[dict]) -> None:
    """An addressee of buttons is a person: a bot never presses, the question would hang unanswered."""
    bad = []
    for u in users:
        if not u.get("is_bot"):
            continue
        own = None
        try:
            own = d.call("GET", "/users/" + d.call("GET", "/bots/" + u["id"])["owner_id"]).get("username")
        except Exception:
            pass
        bad.append(f"@{u['username']}" + (f" — its person: `--to {own}`" if own else ""))
    if bad:
        raise Refused(6, "--ask --to: a bot does not press buttons — " + "; ".join(bad) +
                         ". The addressee of a question is a person (login — `lmost who <name>`).")


def _members(d: Draft, ids: Dict[str, str]) -> None:
    """A mentioned person and an addressee must be members of the post's channel: otherwise the mention notifies nobody
    and the buttons are not seen. Direct and group messages — skipped."""
    if not ids or not d.channel_id:
        return
    try:
        ch = d.call("GET", f"/channels/{d.channel_id}")
        if ch.get("type") in ("D", "G"):
            return
        mem = {m.get("user_id") for m in (d.call("POST", f"/channels/{d.channel_id}/members/ids",
                                                  list(ids.values())) or [])}
    except Exception as x:
        _warn(d, f"channel members not checked ({str(x)[:120]})")
        return
    out = [n for n, uid in ids.items() if uid not in mem and uid != d.me]
    if not out:
        return
    if d.invite:
        for n in out:
            d.call("POST", f"/channels/{d.channel_id}/members", {"user_id": ids[n]})
        _warn(d, f"added to channel {ch.get('name')}: " + ", ".join("@" + n for n in out))
        return
    raise Refused(7, f"not members of channel {ch.get('name')}: " + ", ".join("@" + n for n in out) +
                     " — the mention will not notify them, they will not see buttons. Add them and post — the same call "
                     "with `--invite`.")


BUILTIN: List[Callable[[Draft], None]] = [questions_as_text, private_guard, logins]


def run(d: Draft, plugins: Optional[List[Callable[[Draft], None]]] = None) -> Draft:
    """The pipeline: built-ins, then plugins (``TEXT_CHECKS`` unless given). Raises ``Refused``; notes → ``d.notes``."""
    for fn in BUILTIN:
        fn(d)
    for fn in TEXT_CHECKS if plugins is None else plugins:
        name = getattr(fn, "__name__", repr(fn))
        try:
            fn(d)
        except Refused as r:
            if r.code not in PLUGIN_CODES:
                raise Refused(20, f"{r.hint} (text check {name} used code {r.code}; plugins refuse with 20–39)")
            raise
        except Exception as x:               # its backend is down: the text goes as is
            _warn(d, f"text check {name} skipped ({type(x).__name__}: {str(x)[:120]})")
    return d


def print_notes(d: Draft, prog: str = "lmost post") -> None:
    for n in d.notes:
        print(f"{prog}: {n}", file=sys.stderr)


# ── a document as post text ────────────────────────────────────────────────────────────────────────────────────────
POST_MAX = 15000


def md_chunks(text: str, limit: int = POST_MAX) -> List[str]:
    """Markdown split into parts of up to ~limit characters along lines; preferably before a heading (once the part is
    over half full); a code block is never cut silently: a part ending inside ``` is closed, the next one reopens it."""
    out, cur, fence, size = [], [], False, 0
    for line in text.split("\n"):
        heading = not fence and re.match(r"#{1,3} ", line)
        if cur and (size + len(line) + 1 > limit or (heading and size > limit / 2)):
            out.append(("\n".join(cur), fence))
            cur, size = (["```"] if fence else []), (4 if fence else 0)
        cur.append(line)
        size += len(line) + 1
        if line.lstrip().startswith("```"):
            fence = not fence
    if cur:
        out.append(("\n".join(cur), fence))
    return [c + ("\n```" if f else "") for c, f in out] or [""]
