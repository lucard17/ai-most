"""Questions with buttons (spec: questions): the ask spec, how a question post looks, cancelling one.

Pure functions — the posting itself is ``post.Poster.ask``; the edge redraws a pressed question with the same ``render``.

An ask spec — ``{"context": "…", "parts": [{"title", "text", "options": […], "rec": N}], "to": [logins],
"mention": [logins], "discuss": bool, "sink": "<answer-sink key>"}``. ``rec`` is an option number (1…). Each button's
context carries only what the edge needs to find and verify the question — the spec and answers live in the post's
props (``aimost_ask``, ``aimost_answers``): a context with the whole spec, repeated per button, made big questions exceed
the server's request limit.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Dict, List, Optional, Tuple

from .textcheck import mentioned
from .names import P

TITLE_MAX = 300
CUSTOM = "✍️ own answer"
DISCUSS = "💬 discuss in DM"


class AskError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def new_id() -> str:
    return "ask-" + secrets.token_hex(4)


def norm_rec(spec: dict) -> dict:
    """``rec`` to an option number: a number string or the option text are accepted; anything else — refusal (the edge
    compares a number, and a string ``rec`` silently lost its ⭐)."""
    for n, part in enumerate(spec.get("parts") or [], 1):
        r, opts = part.get("rec"), [str(o) for o in part.get("options") or []]
        if r is None or isinstance(r, int) and not isinstance(r, bool) and 1 <= r <= len(opts):
            continue
        if isinstance(r, str) and r.strip().isdigit() and 1 <= int(r) <= len(opts):
            part["rec"] = int(r)
        elif isinstance(r, str) and r in opts:
            part["rec"] = opts.index(r) + 1
        else:
            raise AskError(f"item {n}: rec={r!r} is neither an option number 1…{len(opts)} nor an option's text")
    return spec


def from_args(rest: List[str]) -> dict:
    """``"<question>" "<option>"… [--rec N]`` or ``<spec>.json`` → normalised spec."""
    rest = list(rest)
    rec = None
    if "--rec" in rest:
        i = rest.index("--rec")
        if i + 1 >= len(rest):
            raise AskError("--rec <option number>")
        rec, rest = rest[i + 1], rest[:i] + rest[i + 2:]
    if len(rest) == 1 and rest[0].endswith(".json"):
        try:
            with open(rest[0], encoding="utf-8") as f:
                spec = json.load(f)
        except (OSError, ValueError) as x:
            raise AskError(f"--ask {rest[0]}: {x}")
    elif len(rest) >= 3:
        part = {"title": rest[0], "text": "", "options": rest[1:]}
        if rec is not None:
            part["rec"] = int(rec) if str(rec).isdigit() else rec
        spec = {"context": "", "parts": [part]}
    else:
        raise AskError("--ask: \"<question>\" and at least two options, or a .json spec")
    return check(norm_rec(spec))


def check(spec: dict) -> dict:
    parts = spec.get("parts")
    if not isinstance(parts, list) or not parts:
        raise AskError("--ask spec: \"parts\" — a non-empty list")
    for k, part in enumerate(parts, 1):
        if len(part.get("options") or []) < 2:
            raise AskError(f"--ask: item {k} needs at least two options")
        t = part.get("title") or ""
        if not t.strip():
            raise AskError(f"--ask: item {k} has no question (title)")
        if len(t) > TITLE_MAX or "\n" in t.strip():
            raise AskError(
                f"--ask: question{f' {k}' if len(parts) > 1 else ''} — {len(t)} characters"
                f"{' with line breaks' if chr(10) in t.strip() else ''}: a question is one sentence up to {TITLE_MAX} "
                "characters. Context — a separate post (`aimost post --file x.md`) or the text/context fields of a .json "
                "spec ({\"context\": …, \"parts\": [{\"title\": <question>, \"text\": <explanation>, …}]}).", 4)
        part.setdefault("text", "")
    spec.setdefault("context", "")
    return spec


def action_url(conn: dict) -> str:
    """Where Mattermost sends a press: the edge as the chat server sees it (``EDGE_PUBLIC``: host:port or a URL)."""
    pub = (conn.get("EDGE_PUBLIC") or "aimost-edge:8767").rstrip("/")
    return (pub if "://" in pub else "http://" + pub) + "/action"


def client_auth(conn: dict) -> dict:
    """Button signature on the bridge side: the owner's key and its id."""
    a = {"secret": conn.get("EDGE_SECRET", "")}
    if conn.get("EDGE_KEY_ID"):
        a["kid"] = conn["EDGE_KEY_ID"]
    return a


def _shown(part: dict, a: dict) -> str:
    return part["options"][a["option"] - 1] if a.get("option") else "«" + (a.get("text") or "") + "»"


def render(q: str, spec: dict, answers: Dict[str, dict], agent: Optional[str], auth: dict,
           url: str) -> Tuple[str, List[dict]]:
    """A question post: (message, attachments). Per item — the chosen answer or the buttons. ``answers`` —
    ``{"<item>": {option | text, by}}``; ``auth`` signs the buttons; ``url`` — the edge action URL."""
    atts = []
    parts = spec["parts"]
    for n, part in enumerate(parts, 1):
        a = answers.get(str(n))
        att = {"title": f"{n}. {part['title']}", "text": part.get("text") or ""}
        if a:
            att["text"] += f"\n✅ **{_shown(part, a)}** — @{a['by']}"
        else:
            ctx = {"q": q, "part": n, **auth, "agent": agent}
            att["actions"] = [{"id": f"p{n}o{m}", "name": ("⭐ " if m == part.get("rec") else "") + str(opt),
                               "type": "button", "integration": {"url": url, "context": dict(ctx, option=m)}}
                              for m, opt in enumerate(part["options"], 1)]
            att["actions"].append({"id": f"p{n}custom", "name": CUSTOM, "type": "button",
                                   "integration": {"url": url, "context": dict(ctx, custom=True)}})
        atts.append(att)
    done = len(answers) >= len(parts)
    if spec.get("discuss") and not done:
        ctx = {"kind": "discuss", "q": q, **auth, "to": spec.get("to"), "agent": agent}
        atts.append({"text": "", "actions": [{"id": "discuss", "name": DISCUSS, "type": "button",
                                              "integration": {"url": url, "context": ctx}}]})
    context = spec.get("context") or ""
    if done:
        res = [f"{p['title']} → **{_shown(p, answers[str(n)])}** — @{answers[str(n)]['by']}"
               for n, p in enumerate(parts, 1)]
        return "✅ " + (context + "\n" if context else "") + "\n".join(res), atts
    head = "❓ " + context
    if spec.get("mention"):
        # addressees: mentioned in the first line and the question itself in the post body — a mention inside an
        # attachment card notifies nobody; someone already mentioned in the text is not repeated
        said = context + " " + " ".join(p.get("title") or "" for p in parts)
        tag = [u for u in spec["mention"] if not mentioned(said, u)]
        titles = [(t if len(t) <= TITLE_MAX else t[:TITLE_MAX - 1] + "…") for t in
                  (" ".join(p["title"].split()) for p in parts)]
        ask = titles[0] if len(titles) == 1 else "\n".join(f"{n}. {t}" for n, t in enumerate(titles, 1))
        head = ("".join(" @" + u for u in tag)[1:] + "\n" if tag else "") + "❓ " + (context + "\n" if context else "") + ask
    return head + f"\n_Answered {len(answers)} of {len(parts)}._", atts


def title(spec: dict) -> str:
    """One line for the registry and lists: the first item's question (+N more)."""
    parts = spec.get("parts") or [{}]
    t = " ".join((parts[0].get("title") or spec.get("context") or "").split())[:120]
    return t + (f" (+{len(parts) - 1})" if len(parts) > 1 else "")


def cancelled_patch(post: dict, why: str, by: Optional[str] = None, at: Optional[int] = None) -> dict:
    """Patch body that cancels a question in place: buttons removed (items and given answers stay), the message starts
    with "⏹ Question cancelled: <why>", props get ``aimost_cancelled``. A patch replaces props whole — all are copied."""
    at = int(time.time() * 1000) if at is None else at
    props = dict(post.get("props") or {})
    props["attachments"] = [{k: v for k, v in a.items() if k != "actions"} for a in props.get("attachments") or []]
    props[P("cancelled")] = {"why": why, "at": at, **({"by": by} if by else {})}
    return {"message": f"⏹ Question cancelled: {why}\n" + (post.get("message") or ""), "props": props}
