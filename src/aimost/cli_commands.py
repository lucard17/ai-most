"""``aimost commands`` — the slash command registry: list it, register it in Mattermost (commands spec).

``aimost commands register --team <name>`` prints the slash commands with keyed URLs; with ``--create`` it creates or updates
them using an admin token from ``AIMOST_MM_TOKEN`` (never on the command line). The URL key comes from ``EDGE_SLASH_SECRET``
(or ``EDGE_SECRET``) — the same value the edge has; the edge address — ``--public`` or ``EDGE_PUBLIC``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List

from . import commands, mattermost


def _list() -> int:
    for name in commands.names():
        for c in commands.subcommands(name).values():
            label = " ".join(x for x in (f"/{name}", c.sub, c.args) if x)
            print(f"{label:<44} {c.kind:<8} {','.join(c.who):<14} {c.help}")
    return 0


def _register(a: argparse.Namespace) -> int:
    secret = os.environ.get("EDGE_SLASH_SECRET") or os.environ.get("EDGE_SECRET")
    public = a.public or os.environ.get("EDGE_PUBLIC")
    if not secret or not public:
        print("aimost commands: need EDGE_SLASH_SECRET (or EDGE_SECRET) and --public / EDGE_PUBLIC", file=sys.stderr)
        return 2
    if not a.create:
        print(json.dumps(commands.registration(public, secret, a.team), ensure_ascii=False, indent=1))
        return 0
    token, url = os.environ.get("AIMOST_MM_TOKEN"), a.url or os.environ.get("AIMOST_MM_URL")
    if not token or not url:
        print("aimost commands: --create needs AIMOST_MM_TOKEN (admin) and --url / AIMOST_MM_URL", file=sys.stderr)
        return 2
    conn = {"URL": url, "BOT_TOKEN": token}
    team = mattermost.api(conn, "GET", f"/teams/name/{a.team}")
    have = mattermost.api(conn, "GET", f"/commands?team_id={team['id']}&custom_only=true") or []
    existing = {c["trigger"]: c for c in have if not c.get("delete_at")}
    for body in commands.registration(public, secret, team["id"]):
        old = existing.get(body["trigger"])
        if old:
            mattermost.api(conn, "PUT", f"/commands/{old['id']}", dict(old, **body))
            print(f"updated /{body['trigger']}")
        else:
            mattermost.api(conn, "POST", "/commands", body)
            print(f"created /{body['trigger']}")
    return 0


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="aimost commands", description="slash command registry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="commands, kind, who may call")
    r = sub.add_parser("register", help="print (or --create) the Mattermost slash commands")
    r.add_argument("--team", required=True, help="team name")
    r.add_argument("--public", help="the edge as Mattermost sees it, host:port (default EDGE_PUBLIC)")
    r.add_argument("--create", action="store_true", help="create or update them (AIMOST_MM_TOKEN, --url)")
    r.add_argument("--url", help="Mattermost URL for --create (default AIMOST_MM_URL)")
    a = ap.parse_args(argv)
    return _list() if a.cmd == "list" else _register(a)
