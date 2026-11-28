"""``aimost edge serve`` — run the edge next to Mattermost (edge spec); configuration from the environment (``aimost.edge``)."""

from __future__ import annotations

import argparse
import sys
from typing import List

from . import __version__


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="aimost edge", description="the HTTP endpoint Mattermost calls: buttons, dialogs, "
                                 "slash commands. Configured by EDGE_* environment variables.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="listen on EDGE_LISTEN")
    sub.add_parser("check", help="check the configuration and the bot token, then exit")
    sub.add_parser("version", help="print the edge version")
    sub.add_parser("loop-once", help="one pass of the loop arbiter, then exit")
    sub.add_parser("loop-clear", help="lift every pause the loop arbiter set")
    a = ap.parse_args(argv)
    if a.cmd == "version":
        print(__version__)
        return 0
    from . import edge
    e = edge.Edge()
    err = e.check()
    if err:
        print(f"aimost edge: {err}", file=sys.stderr)
        return 2
    if a.cmd in ("loop-once", "loop-clear"):
        from . import looparb
        arb = looparb.arbiter(e)
        arb.pass_() if a.cmd == "loop-once" else arb.clear()
        return 0
    if a.cmd == "check":
        try:
            e.me()
        except Exception as x:
            print(f"aimost edge: Mattermost {e.mm_url} with EDGE_BOT_TOKEN: {type(x).__name__}", file=sys.stderr)
            return 1
        print(f"ok: Mattermost {e.mm_url}, buttons call {e.public}")
        return 0
    edge.serve(e)
    return 0
