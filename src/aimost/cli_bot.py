"""``aimost bot add`` — create a role's bot and admit its session to the connection.

  aimost bot add <role> --desc "<what it does>" [--shared | --owner <login>] [--prefix <p>] [--channel <c>]… [--via C]
  aimost bot add <role> --enable [--prefix <p>] [--shared] [--via C]   the role's bot is disabled — enable it
  aimost bot add <role> --admit [--via C]      the token is in <config>/bots/<conn>/<role> (0600): admit the session

With an admin token (``AIMOST_ADMIN_TOKEN`` or ``<config>/admin/<connection>``) the bot is created here. Without one the
bot is ASKED for: a ``bot-create`` / ``bot-enable`` request of the ``admin`` plugin (``aimost admin``), executed by the edge
after the confirmer's press; the owner then takes the token with ``/bot-token <bot>``, puts it into the token file and runs
``--admit``. ``--enable`` enables only a bot of the edge owner. A retry is safe.
Exit codes: 1 the role already has a bot, 2 usage, 3 no token received, 4 the name is a person's account,
5 the team refused the bot, 6 ``--owner`` is not a person, 7 the name is a bot owned by someone else.
"""

from __future__ import annotations

import sys
from typing import List

from . import botadmin, connections, mattermost


def _err(msg: str, code: int) -> int:
    print("aimost bot: " + msg, file=sys.stderr)
    return code


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[0] != "add" or any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 2
    role, rest = argv[1], argv[2:]
    o = {"desc": "", "owner": "", "prefix": "", "via": connections.default_name() or ""}
    shared, chans, enable, admit = False, [], False, False
    while rest:
        a = rest[0]
        if a == "--shared":
            shared, rest = True, rest[1:]
        elif a == "--enable":
            enable, rest = True, rest[1:]
        elif a == "--admit":
            admit, rest = True, rest[1:]
        elif a == "--channel" and len(rest) >= 2:
            chans.append(rest[1])
            rest = rest[2:]
        elif a in ("--desc", "--owner", "--prefix", "--via") and len(rest) >= 2:
            o[a[2:]] = rest[1]
            rest = rest[2:]
        else:
            return _err(f"unknown argument {a!r}", 2)
    conn = connections.connections().get(o["via"])
    if not conn:
        return _err(f"no connection {o['via']!r}", 2)
    if admit:
        return _admit(conn, role)
    if enable and o["owner"]:
        return _err("--enable --owner: only bots of the edge owner are enabled", 2)
    admin = botadmin.admin_token(conn)
    if not admin or enable:
        return _ask(conn, role, o, shared, chans, enable)

    def api(method: str, path: str, body: object = None) -> object:
        return mattermost.api(conn, method, path, body, token=admin)

    try:
        out = botadmin.add(conn, api, role, o["desc"], shared, o["owner"], o["prefix"], chans)
    except botadmin.Refused as e:
        return _err(str(e), e.code)
    except mattermost.HTTPError as e:
        return _err(f"chat refused: {e}", 3)
    if not o["owner"]:
        tok = botadmin.token_path(conn, role).read_text().strip()
        try:
            me = mattermost.api(conn, "GET", "/users/me", token=tok)["username"]
        except mattermost.HTTPError as e:
            return _err(f"token written but does not answer: {e}", 3)
        out += f" — checked: @{me}"
    print("aimost bot: " + out)
    return 0


def _admit(conn, role: str) -> int:
    from . import paths
    tok = botadmin.token_path(conn, role)
    if not tok.is_file() or not tok.read_text().strip():
        return _err(f"no token in {tok} — first /bot-token <bot> and put it there (0600)", 3)
    if tok.stat().st_mode & 0o077:
        return _err(f"{tok} is readable by others — chmod 600 and retry", 3)
    botadmin.admit_session(paths.config_dir() / "connections" / f"{conn['NAME']}.env", role)
    botadmin.restart_bridge()
    try:
        me = mattermost.api(conn, "GET", "/users/me", token=tok.read_text().strip())["username"]
    except mattermost.HTTPError as e:
        return _err(f"admitted, but the token does not answer: {e}", 3)
    print(f"aimost bot: {role} → @{me} (connection {conn['NAME']}, token {tok})")
    return 0


def _ask(conn, role: str, o: dict, shared: bool, chans, enable: bool) -> int:
    """No admin token: ask the edge (the admin plugin's request)."""
    try:
        from aimost_admin import request
    except ImportError:
        return _err("no admin token and no admin plugin", 2)
    if not enable and not o["desc"].strip():
        return _err('need --desc "<what the agent does, what to bring to it>"', 2)
    owner = o["owner"] or conn.get("OWNER", "")
    if not owner:
        return _err(f"no OWNER in connection {conn['NAME']} — nobody to own the bot", 2)
    name = botadmin.bot_name(conn, role, shared, o["owner"], o["prefix"])
    req = request.bot_request(conn, name, owner, o["desc"], chans, enable)
    try:
        out = request.send(conn, req)
    except request.RequestError as e:
        return _err(str(e), e.code)
    tok = botadmin.token_path(conn, role)
    then = (f"after the press the owner takes the token: /bot-token {name}" if o["owner"] else
            f"after the press: /bot-token {name}, the token into {tok} (chmod 600), then `aimost bot add {role} --admit`")
    print(f"aimost bot: asked — {out}\n{then}")
    return 0
