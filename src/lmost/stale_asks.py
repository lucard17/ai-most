"""Stop reminder to cancel stale questions (spec: questions).

When the owner spoke *in words* during the turn and the session still has open questions to the owner, the delivery
hook stops the turn once with the list: answered already — cancel it; still needed — keep it and say why.

The owner's words in a turn — any of: the turn was started by input typed into the terminal (``origin.kind=human``);
input typed mid-turn (``queued_command`` with ``origin.kind=human``); the inbox handed out the owner's chat message or
free-text answer during the turn (mark ``<marks>/<role>.owner-words``, written by ``Inbox.unread(ack=True)``). A button
answer is not words: it closes its own question. Questions the owner snoozed in "Waiting for you" are left out. Once per
turn: ``<marks>/<role>.stale-asked`` holds the id of the turn start.
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

from . import askstore, connections, paths, turn_origin


def _age(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // 1440} d"


def reminder(role: str, transcript: str, now: Optional[float] = None) -> Optional[str]:
    now = time.time() if now is None else now
    recs = turn_origin.tail(transcript)
    start = turn_origin.turn_start(recs)
    if start is None:
        return None
    first = recs[start]
    turn = str(first.get("uuid") or turn_origin.ts(first))
    words = (first.get("origin") or {}).get("kind") == "human" or any(
        r.get("type") == "attachment" and (r.get("attachment") or {}).get("type") == "queued_command"
        and ((r.get("attachment") or {}).get("origin") or {}).get("kind") == "human" for r in recs[start:])
    marks = paths.marks_dir()
    if not words:
        try:
            words = os.path.getmtime(marks / f"{role}.owner-words") >= turn_origin.ts(first) - 1
        except OSError:
            pass
    if not words:
        return None
    mark = marks / f"{role}.stale-asked"
    try:
        if mark.read_text().strip() == turn:
            return None
    except OSError:
        pass
    conns = connections.connections()
    snoozed: Dict[str, dict] = {}
    rows: List[str] = []
    for q, r in askstore.asks(role).items():
        via = r.get("via") or ""
        owner = (conns.get(via) or {}).get("OWNER")
        if r.get("state", "open") != "open" or not owner or owner not in (r.get("to") or []):
            continue
        if via not in snoozed:
            snoozed[via] = askstore.snoozes(via)
        if q in snoozed[via]:
            continue
        m = max(0, int((now * 1000 - (r.get("at") or now * 1000)) / 60000))
        title = " ".join(str(r.get("title") or "").split())[:100]
        rows.append(f"- {q} · waiting {_age(m)} · «{title}»")
    if not rows:
        return None
    paths.private_dir(marks)
    mark.write_text(turn)
    return ("🧹 The owner spoke in words this turn, and the session has open questions to them:\n" + "\n".join(rows)
            + "\nDecide on each: already answered — cancel it with `lmost post --cancel <ask-id> \"<why>\"`; still "
              "needed — keep it and say why in one line. This reminder comes once per turn.")
