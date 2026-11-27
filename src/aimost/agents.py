"""Agent directory: which agent bots exist in the chat, whose they are, what they do (status spec).

The source is the description in each bot's profile — the directory is built from it, never kept by hand. Published as a
pinned post (prop ``aimost_agents``) that is edited in place; when nothing changed, nothing is written.

Connection keys: ``BOT_OWNERS`` — ``<prefix>:<login>,…`` (a bot named ``<prefix>-…`` belongs to the login; without a
prefix the bot is shared), ``BOT_SERVICE`` — service bots (buttons, webhooks) listed apart, ``PRIVATE_GUARD=on`` — the
text is checked against the private patterns before it is published.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

from .connections import Connection, _split

MARK = "aimost_agents"
SHARED = "shared"
DESC_MAX = 180

Api = Callable[..., object]


def bots(conn: Connection, api: Api) -> List[dict]:
    """Active bots in the connection's team (deleted ones excluded)."""
    team = api("GET", f"/teams/name/{conn['TEAM']}")
    out: List[dict] = []
    page = 0
    while True:
        part = api("GET", f"/users?in_team={team['id']}&per_page=200&page={page}") or []
        out += [u for u in part if u.get("is_bot") and not u.get("delete_at")]
        if len(part) < 200:
            return out
        page += 1


def owner_of(name: str, conn: Connection) -> str:
    pref: Dict[str, str] = {}
    for x in _split(conn.get("BOT_OWNERS", "").replace(" ", "")):
        p, _, login = x.partition(":")
        if p and login:
            pref[p] = login
    for p, login in sorted(pref.items(), key=lambda kv: -len(kv[0])):
        if name.startswith(p + "-"):
            return "@" + login
    return SHARED


def clean(text: Optional[str]) -> str:
    t = re.sub(r"\s+", " ", text or "").strip().replace("|", "/")
    return (t[:DESC_MAX] + "…") if len(t) > DESC_MAX else t


def table(conn: Connection, users: List[dict]) -> str:
    service = set(_split(conn.get("BOT_SERVICE", "")))
    rows, svc = [], []
    key: Callable[[dict], Tuple] = lambda u: (owner_of(u["username"], conn) != SHARED, owner_of(u["username"], conn),
                                              u["username"])
    for u in sorted(users, key=key):
        if u["username"] in service:
            svc.append("@" + u["username"])
            continue
        desc = clean(u.get("bot_description")) or "no description"
        rows.append(f"| @{u['username']} | {owner_of(u['username'], conn)} | {desc} |")
    text = ("📇 **Agents in this chat** — built from the bots' profiles\n\n"
            "| Bot | Whose | What it does |\n| --- | --- | --- |\n" + "\n".join(rows) + "\n\n"
            "**How to reach one:** mention the bot in a thread or write to it directly — the message goes to its session, "
            "the answer comes back to the same thread. A request to someone else's agent is a request, not an order: "
            "merges, deploys and production work happen only on its owner's word.")
    if svc:
        text += "\n\n_Service bots (buttons, notifications; they do not answer):_ " + ", ".join(svc)
    return text


def publish(conn: Connection, api: Api, channel: str, text: str) -> Tuple[str, str]:
    """Pin the directory in a channel: edit our pinned post in place, or create and pin one.
    Returns (outcome, post id), outcome — unchanged | updated | created."""
    team = api("GET", f"/teams/name/{conn['TEAM']}")
    ch = api("GET", f"/teams/{team['id']}/channels/name/{channel}")
    me = api("GET", "/users/me")["id"]
    pinned = api("GET", f"/channels/{ch['id']}/pinned") or {}
    mine = [p for p in (pinned.get("posts") or {}).values()
            if p.get("user_id") == me and (p.get("props") or {}).get(MARK)]
    if mine:
        p = max(mine, key=lambda x: x["create_at"])
        if p.get("message") == text:
            return "unchanged", p["id"]
        api("PUT", f"/posts/{p['id']}/patch", {"message": text})
        return "updated", p["id"]
    p = api("POST", "/posts", {"channel_id": ch["id"], "message": text, "props": {MARK: True}})
    api("POST", f"/posts/{p['id']}/pin")
    return "created", p["id"]
