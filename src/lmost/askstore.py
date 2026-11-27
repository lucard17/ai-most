"""Storage shared by questions, the bridge and the Stop hook: the question registry, snoozes and DM discussions.

Question registry — ``<state>/asks/<role>.jsonl``, append-only; records with the same ask id fold into one (last field
wins). Needed because the server cannot find a post by ask id: ``--cancel ask-…``, ``--open-asks`` and ``--ttl`` work from
here. Fields: ``q`` (ask id), ``post``, ``via``, ``thread``, ``at`` (ms), ``title``, ``ttl`` (hours), ``to`` (addressees),
``blocking``, ``state`` (open | cancelled | answered | gone), ``cancelled_at``, ``why``, ``sink`` (answer-sink key).

Post props of a question (written by ``lmost post``, read by the edge and the bridge):
``lmost_question`` (ask id), ``lmost_ask`` (spec with ``parts``), ``lmost_answers`` ({part: answer}),
``lmost_cancelled`` ({why, at, by}), ``agent`` (asking role), ``relay`` (relay bot posted it).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import paths


def asks_dir() -> Path:
    return Path(os.environ.get("LMOST_ASKS_DIR") or paths.state_dir() / "asks")


def ask_record(role: str, rec: dict) -> None:
    d = paths.private_dir(asks_dir())
    p = d / f"{role}.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.chmod(p, 0o600)


def asks(role: str) -> Dict[str, dict]:
    """The role's questions: ask id → folded record, in the order they were asked."""
    out: Dict[str, dict] = {}
    try:
        with open(asks_dir() / f"{role}.jsonl", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("q"):
                    out.setdefault(r["q"], {}).update(r)
    except OSError:
        pass
    return out


def open_asks(role: str) -> List[dict]:
    return [r for r in asks(role).values() if r.get("state", "open") == "open"]


def ask_roles() -> List[str]:
    return sorted(p.name[:-6] for p in asks_dir().glob("*.jsonl"))


def ask_state(post: dict) -> str:
    """State of a question post: cancelled | answered (every part) | open."""
    props = post.get("props") or {}
    if props.get("lmost_cancelled"):
        return "cancelled"
    parts = (props.get("lmost_ask") or {}).get("parts")
    if parts and len(props.get("lmost_answers") or {}) >= len(parts):
        return "answered"
    return "open"


# Snoozed lines of "Waiting for you": key (ask id, or "<sink>:<id>" for a sink question) → {until (ms; null — until
# brought back), by, at, asker}. Written by the bridge; read by the pin, reminders and the Stop hook.
def snooze_path(conn_name: str) -> Path:
    return paths.link_state_dir() / f"{conn_name}.snooze.json"


def snoozes(conn_name: str, now_ms: Optional[float] = None) -> Dict[str, dict]:
    """Snoozed now: key → record; an expired snooze is not snoozed."""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    try:
        d = json.loads(snooze_path(conn_name).read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in (d.get("items") or {}).items() if v.get("until") is None or v["until"] > now_ms}


# Discussions of questions in a DM ("💬 discuss in DM"): DM thread → question. Written by the bridge, read by
# ``lmost post --conclude``.
def discussions_path(conn_name: str) -> Path:
    return paths.link_state_dir() / f"{conn_name}.discuss.json"


def discussions(conn_name: str) -> dict:
    try:
        return json.loads(discussions_path(conn_name).read_text())
    except (OSError, ValueError):
        return {}
