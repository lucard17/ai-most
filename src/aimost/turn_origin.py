"""Reading a Claude Code transcript: who started the current turn, whether the turn has ended, whether the owner typed
into the terminal mid-turn. Used by the delivery hook, the peer guard, the stale-questions reminder and the dispatcher.

Turn start — the last ``type=user`` record whose ``origin.kind`` is ``human`` or ``peer`` (tool results and task
notifications do not start a turn):

    human  input typed into the session (by the owner, or an automatic prompt typed into the pane) — except a wake-up;
           it only ever *clears* a peer mark set before the turn, it grants nothing;
    wake   an inbox wake-up: input exactly ``/inbox`` (or the ``/inbox`` command, as ``claude --resume … "/inbox"`` writes
           it), or a message of another session whose body *starts* with ``aimost dispatch →`` (a search anywhere in the
           text would let a pasted line turn the owner's turn into a wake-up);
    peer   a message of another session;
    none   no transcript or no turn start.

Only the tail of the transcript is read (transcripts can be hundreds of MB).
"""

from __future__ import annotations

import calendar
import json
import re
import time
from typing import List, Optional, Tuple

TAIL_LINES = 4000
TAIL_BYTES = 4_000_000
WAKE_PREFIX = "aimost dispatch →"
# Records that do not tell whether a turn is running: queue bookkeeping of background work, metadata.
_NOISE = {"queue-operation", "pr-link", "file-history-snapshot", "file-history-delta", "custom-title", "summary", "mode",
          "permission-mode", "atis-latch", "last-prompt", "tag", "agent-setting"}


def ts(rec: dict) -> int:
    """Unix time of a record's ``timestamp`` (UTC, ISO 8601); 0 if absent or broken."""
    v = str(rec.get("timestamp") or "")
    try:
        return calendar.timegm(time.strptime(v[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def tail(path: str, lines: int = TAIL_LINES) -> List[dict]:
    """The last records of a transcript, parsed; broken lines (a cut in the middle of a character) are skipped."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read()
    except OSError:
        return []
    out = []
    for line in raw.decode("utf-8", "replace").splitlines()[-lines:]:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _text(rec: dict) -> str:
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    return " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))


def _kind(rec: dict) -> Optional[str]:
    if rec.get("type") != "user":
        return None
    k = (rec.get("origin") or {}).get("kind")
    return k if k in ("human", "peer") else None


def turn_start(recs: List[dict]) -> Optional[int]:
    """Index of the record that started the current turn, or None."""
    for i in range(len(recs) - 1, -1, -1):
        if _kind(recs[i]):
            return i
    return None


def classify(rec: dict) -> str:
    k = _kind(rec)
    t = _text(rec).strip()
    if k == "human" and (t == "/inbox" or t.startswith("<command-message>inbox</command-message>")
                         or t.startswith("<command-name>/inbox</command-name>")):
        return "wake"
    if k == "peer":
        body = re.sub(r"^.*?<cross-session-message[^>]*>\s*", "", t, count=1, flags=re.S)
        return "wake" if body.startswith(WAKE_PREFIX) else "peer"
    return "human" if k == "human" else "none"


def origin(path: str) -> Tuple[str, int]:
    """(who started the current turn, its start time): human | wake | peer | none."""
    recs = tail(path)
    i = turn_start(recs)
    if i is None:
        return "none", 0
    return classify(recs[i]), ts(recs[i])


def owner_typed_since(path: str, since: float) -> bool:
    """The owner typed into the terminal mid-turn at or after ``since``: Claude Code merges such input into the running
    turn as an ``attachment`` of type ``queued_command`` with ``origin.kind=human`` (messages of sessions are ``peer``).
    A chat post cannot produce such a record."""
    for r in tail(path):
        a = r.get("attachment") or {}
        if r.get("type") == "attachment" and a.get("type") == "queued_command" \
                and (a.get("origin") or {}).get("kind") == "human" and ts(r) >= int(since):
            return True
    return False


# Session-lifecycle commands that tools print into the pane (a restart prints /wake-up, self-compaction /compact and
# /wake-up): typed by a program, not the owner's word to the session.
LIFECYCLE = re.compile(r"^\s*(?:/wake-up\s*$|/(?:compact|self-compact)\b|<command-message>(?:wake-up|compact|self-compact)"
                       r"</command-message>|<command-name>/(?:wake-up|compact|self-compact)</command-name>)")


def typed_in_turn(path: str) -> int:
    """When the owner last typed into the session in the current turn (epoch; 0 — not in this turn): the turn start when it
    is ``human``, else the newest mid-turn ``queued_command`` of ``origin.kind=human`` after it. A wake-up or a session's
    message starting the turn, typing in an earlier turn, and a lifecycle command a tool printed (``LIFECYCLE``) do not
    count."""
    recs = tail(path)
    i = turn_start(recs)
    if i is None:
        return 0
    at = ts(recs[i]) if classify(recs[i]) == "human" and not LIFECYCLE.match(_text(recs[i])) else 0
    for r in recs[i + 1:]:
        a = r.get("attachment") or {}
        if r.get("type") == "attachment" and a.get("type") == "queued_command" \
                and (a.get("origin") or {}).get("kind") == "human" and not LIFECYCLE.match(str(a.get("prompt") or "")):
            at = max(at, ts(r))
    return at


def turn_ended(path: str) -> bool:
    """The session's turn is over: the last meaningful record is the end-of-turn system record. ``claude agents`` keeps
    a session "busy" while background work runs after the turn, and that work's queue records keep the transcript
    changing — they are skipped."""
    last = None
    for r in tail(path, lines=2000):
        if r.get("type") in _NOISE:
            continue
        last = r
    return bool(last) and last.get("type") == "system" and last.get("subtype") in ("turn_duration", "stop_hook_summary")
