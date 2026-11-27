"""Creating role bots (status spec, "One bot per role"; "Bots for other people are created by a chat admin").

Works over REST with a token of a user allowed to create bots (a system admin, or a user granted bot creation):
``AIMOST_ADMIN_TOKEN`` or the file ``<config>/admin/<connection>`` (0600). Nothing runs on the chat server itself.

Bot name: a person's bot is ``<prefix>-<role>`` where the prefix is ``OWNER`` of the connection, ``--owner``'s login or
``--prefix``; a role already starting with ``<prefix>-`` is not prefixed twice; ``--shared`` — no prefix.

Own bot: the token goes to ``<config>/bots/<connection>/<role>`` (0600, never printed) and the role is added to the
connection's ``SESSIONS``. Someone else's bot (``--owner``): the bot is assigned to that person and no token is issued
here — the owner takes one with ``/bot-token <bot>`` (commands spec); the admin's own connection is untouched.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

from . import paths
from .connections import Connection
from .mattermost import HTTPError

ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
LOGIN_RE = re.compile(r"^[a-z0-9._-]{2,60}$")
PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")

Api = Callable[..., object]


class Refused(Exception):
    """The bot is not created; ``code`` is the exit code (see ``cli_bot``)."""

    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def admin_token(conn: Connection) -> Optional[str]:
    if os.environ.get("AIMOST_ADMIN_TOKEN"):
        return os.environ["AIMOST_ADMIN_TOKEN"]
    try:
        return (paths.config_dir() / "admin" / conn["NAME"]).read_text().strip() or None
    except OSError:
        return None


def token_path(conn: Connection, role: str) -> Path:
    return paths.config_dir() / "bots" / conn["NAME"] / role


def bot_name(conn: Connection, role: str, shared: bool = False, owner: str = "", prefix: str = "") -> str:
    if not ROLE_RE.match(role):
        raise Refused(2, f"bad role name {role!r}")
    if shared:
        if owner:
            raise Refused(2, "--owner and --shared do not go together")
        return role
    if owner and not LOGIN_RE.match(owner):
        raise Refused(2, f"bad login {owner!r}")
    if prefix and not PREFIX_RE.match(prefix):
        raise Refused(2, f"bad prefix {prefix!r}")
    pre = prefix or owner or conn.get("OWNER", "")
    if not pre:
        raise Refused(2, f"connection {conn['NAME']} has no OWNER — nothing to build the bot name from")
    return role if role.startswith(pre + "-") else f"{pre}-{role}"


def _get(api: Api, path: str) -> Optional[dict]:
    try:
        return api("GET", path)
    except HTTPError as e:
        if e.code == 404:
            return None
        raise


def write_secret(p: Path, value: str) -> None:
    paths.private_dir(p.parent)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix="." + p.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value)
        os.replace(tmp, p)
    except BaseException:
        os.unlink(tmp)
        raise


def admit_session(conn_file: Path, role: str) -> bool:
    """Add the role to SESSIONS of a connection file; False when it is already there."""
    lines = conn_file.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("SESSIONS="):
            cur = [x.strip() for x in line.split("=", 1)[1].split(",") if x.strip()]
            if role in cur:
                return False
            lines[i] = "SESSIONS=" + ",".join(cur + [role])
            break
    else:
        lines.append("SESSIONS=" + role)
    conn_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def restart_bridge() -> None:
    """The bridge rereads bots on start. A missing unit is fine (try-restart)."""
    if os.environ.get("AIMOST_NO_RESTART"):
        return
    try:
        subprocess.run([os.environ.get("AIMOST_SYSTEMCTL", "systemctl"), "--user", "try-restart", "aimost-link.service"],
                       capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass


def add(conn: Connection, api: Api, role: str, desc: str, shared: bool = False, owner: str = "", prefix: str = "",
        channels: List[str] = ()) -> str:
    """Create (or bring back) the role's bot; returns a one-line outcome. Raises Refused."""
    if not desc.strip():
        raise Refused(2, 'need --desc "<what the agent does, what to bring to it>" — the bot profile and the directory row')
    name = bot_name(conn, role, shared, owner, prefix)
    if not owner and token_path(conn, role).exists():
        raise Refused(1, f"{role} already has a bot ({token_path(conn, role)}) — nothing done")

    owner_id = ""
    if owner:
        u = _get(api, f"/users/username/{owner}")
        if not u:
            raise Refused(6, f"--owner {owner}: no such user in the chat")
        if u.get("is_bot"):
            raise Refused(6, f"--owner {owner} is a bot; a bot's owner is a person (else /bot-token would hand the token to an agent)")
        owner_id = u["id"]

    u = _get(api, f"/users/username/{name}")
    if u and not u.get("is_bot"):
        raise Refused(4, f"@{name} is taken by a person's account — a person's login is never turned into a bot; "
                         "choose another name (--prefix)")
    if u:
        b = api("GET", f"/bots/{u['id']}?include_deleted=true")
        if owner and b.get("owner_id") != owner_id:
            raise Refused(7, f"@{name} exists and its owner is not {owner}: ownership is not moved silently")
        api("POST", f"/bots/{u['id']}/enable")
        api("PUT", f"/bots/{u['id']}", {"username": name, "display_name": name, "description": desc})
        bot_id = u["id"]
    else:
        bot_id = api("POST", "/bots", {"username": name, "display_name": name, "description": desc})["user_id"]

    team = api("GET", f"/teams/name/{conn['TEAM']}")
    try:
        api("POST", f"/teams/{team['id']}/members", {"team_id": team["id"], "user_id": bot_id})
    except HTTPError as e:
        raise Refused(5, f"team {conn['TEAM']} did not take @{name}: {_reason(e)} "
                         "(the bot account exists; a retry after the fix is safe)")
    for c in channels:
        ch = api("GET", f"/teams/{team['id']}/channels/name/{c}")
        api("POST", f"/channels/{ch['id']}/members", {"user_id": bot_id})

    if owner:
        api("POST", f"/bots/{bot_id}/assign/{owner_id}")
        return (f"{role} → @{name}, owner {owner}; they take the token themselves: /bot-token {name} "
                "(any channel, the answer is visible only to them). Your connection is untouched")

    tok = api("POST", f"/users/{bot_id}/tokens", {"description": f"aimost {role}"}).get("token")
    if not tok:
        raise Refused(3, "no token received")
    write_secret(token_path(conn, role), tok)
    admit_session(paths.config_dir() / "connections" / f"{conn['NAME']}.env", role)
    restart_bridge()
    return f"{role} → @{name} (connection {conn['NAME']}, token {token_path(conn, role)})"


def _reason(e: HTTPError) -> str:
    try:
        import json
        return json.loads(e.read() or b"{}").get("message") or str(e)
    except Exception:
        return str(e)
