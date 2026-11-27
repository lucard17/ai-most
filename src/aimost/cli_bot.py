"""``aimost bot add`` — create a role's bot and admit its session to the connection.

  aimost bot add <role> --desc "<what it does>" [--shared | --owner <login>] [--prefix <p>] [--channel <c>]… [--via C]

Needs an admin token: ``AIMOST_ADMIN_TOKEN`` or ``<config>/admin/<connection>``. A retry is safe.
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
    shared, chans = False, []
    while rest:
        a = rest[0]
        if a == "--shared":
            shared, rest = True, rest[1:]
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
    admin = botadmin.admin_token(conn)
    if not admin:
        return _err(f"no admin token (AIMOST_ADMIN_TOKEN or <config>/admin/{conn['NAME']})", 2)

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
