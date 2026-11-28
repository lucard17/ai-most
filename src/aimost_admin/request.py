"""Asking for an admin action from the owner's machine (admin spec). It is only ASKED here: there is no admin password or
token on this machine; the edge on the chat server executes after the confirmer's press.

The request is checked by the same rules as the edge's (``executor.validate`` — one source; the server's list of who may ask
is checked there), posted by the admin role's bot (connection ``ADMIN_BOT``, default ``admin``) into the log channel
(``ADMIN_CHANNEL``, default ``admin-log``) with props ``<prefix>_admin``, then ``/<command> <post id>`` is called by the same bot.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

from aimost import connections, mattermost

from . import executor


class RequestError(Exception):
    def __init__(self, msg: str, code: int = 2):
        super().__init__(msg)
        self.code = code


def normalize(req) -> dict:
    try:
        action, target = executor.validate(req)
    except executor.Refused as e:
        raise RequestError(f"the form is wrong — {e}")
    return {"action": action, "target": target, "why": req["why"].strip(), "origin": req["origin"]}


def send(conn: dict, req: dict, channel: str = "", thread: str = "") -> str:
    req = normalize(req)
    bot = conn.get("ADMIN_BOT") or "admin"
    tok = connections.bot(conn, bot)
    if not tok:
        raise RequestError(f"no bot of the admin role ({bot}) on connection {conn['NAME']}")
    name = channel or conn.get("ADMIN_CHANNEL") or "admin-log"
    try:
        team = mattermost.api(conn, "GET", f"/teams/name/{urllib.parse.quote(conn['TEAM'])}", token=tok)["id"]
        cid = mattermost.api(conn, "GET", f"/teams/{team}/channels/name/{urllib.parse.quote(name)}", token=tok)["id"]
        post = mattermost.api(conn, "POST", "/posts", {
            "channel_id": cid, "root_id": thread or "", "props": {executor.REQ: req},
            "message": f"🛠 admin request: **{req['action']}** — {req['why']} (asks {req['origin']})"}, token=tok)
        r = mattermost.api(conn, "POST", "/commands/execute", {
            "channel_id": cid, "command": f"/{executor.command_name()} {post['id']}"}, token=tok) or {}
    except mattermost.HTTPError as e:
        raise RequestError(f"the chat answered {e.code}", 3)
    except OSError as e:
        raise RequestError(f"the chat is unreachable ({type(e).__name__})", 3)
    return f"admin: request {post['id']} in ~{name}; edge: {r.get('text') or 'no answer — see the request thread'}"


def bot_request(conn: dict, role_bot: str, owner: str, desc: str, channels, enable: bool,
                origin: Optional[str] = None) -> dict:
    """The request ``aimost bot add`` sends without an admin token: create a role's bot, or enable a disabled one."""
    who = origin or conn.get("OWNER") or owner
    if enable:
        return {"action": "bot-enable", "origin": who, "why": f"enable the role bot {role_bot}",
                "target": {"username": role_bot}}
    return {"action": "bot-create", "origin": who, "why": f"role bot {role_bot}",
            "target": {"team": conn.get("TEAM", ""), "username": role_bot, "display_name": role_bot, "description": desc,
                       "owner": owner, "channels": list(channels)}}
