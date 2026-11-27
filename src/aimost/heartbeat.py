"""Bridge heartbeat (status spec): the edge reads it to answer ``/agent status`` and to tell that the owner's machine is
offline, without reaching that machine.

Where. A bot may not patch its own user (props), but it may write its own preferences, and the edge's bot (system admin)
may read anyone's: the heartbeat is the preference ``aimost_heartbeat/state`` of each role bot, a JSON value
{"seen" (ms), "role", "state": running | idle | waiting | not running, "unread", "last_turn" (ms)}. Other people cannot
read it. A missing heartbeat changes nothing visible by itself: only its age is interpreted.
"""

from __future__ import annotations

import json
import time
import urllib.error
from typing import Callable, Optional

from . import mattermost
from .connections import Connection
from .names import P

CATEGORY, NAME = P("heartbeat"), "state"
STALE_S = 180          # older than 3 minutes — the bridge is offline
STATES = ("running", "idle", "waiting", "not running")


def value(role: str, state: str, unread: int = 0, last_turn: Optional[int] = None, now_ms: Optional[int] = None) -> str:
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    return json.dumps({"seen": int(time.time() * 1000) if now_ms is None else int(now_ms), "role": role, "state": state,
                       "unread": int(unread), "last_turn": last_turn}, separators=(",", ":"))


def write(conn: Connection, token: str, role: str, state: str, unread: int = 0, last_turn: Optional[int] = None) -> None:
    """Write the heartbeat of one role with its own bot token."""
    uid = mattermost.api(conn, "GET", "/users/me", token=token)["id"]
    mattermost.api(conn, "PUT", f"/users/{uid}/preferences",
                   [{"user_id": uid, "category": CATEGORY, "name": NAME,
                     "value": value(role, state, unread, last_turn)}], token=token)


def parse(raw: Optional[str]) -> Optional[dict]:
    """{seen, role, state, unread, last_turn} or None — nothing usable."""
    try:
        d = json.loads(raw or "")
        seen = int(d["seen"])
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(d, dict):
        return None
    return {"seen": seen, "role": d.get("role"), "state": d.get("state") or "?", "unread": d.get("unread") or 0,
            "last_turn": d.get("last_turn")}


def fetch(call: Callable[..., object], uid: str) -> Optional[dict]:
    """The heartbeat of a bot user (``call`` — the edge's API call), None — never written."""
    try:
        rows = call("GET", f"/users/{uid}/preferences/{CATEGORY}") or []
    except urllib.error.HTTPError as x:
        if x.code == 404:
            return None
        raise
    return parse(next((r.get("value") for r in rows if r.get("name") == NAME), None))


def age_s(hb: Optional[dict], now: Optional[float] = None) -> Optional[float]:
    if not hb:
        return None
    return max(0.0, (time.time() if now is None else now) - hb["seen"] / 1000)


def offline(hb: Optional[dict], now: Optional[float] = None) -> bool:
    a = age_s(hb, now)
    return a is None or a > STALE_S
