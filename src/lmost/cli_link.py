"""``lmost link`` — the bridge between chat servers and session inboxes (delivery spec).

  lmost link serve            run: websocket per role bot, a pass on events, a full pass every LMOST_LINK_FULL_S
  lmost link once             one full pass over every connection, then exit
  lmost link routes <conn>    the active delivery rules of a connection (its .routes file or the default set)

A broken routes file stops the bridge before the first pass (exit 2, file and line).
"""

from __future__ import annotations

import sys
from typing import List

from . import connections, link


def main(argv: List[str]) -> int:
    if not argv or argv[0] not in ("serve", "once", "routes") or any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 2
    conns = connections.connections()
    try:
        for c in conns.values():
            link.load_routes(c)
    except link.RoutesError as e:
        print(f"lmost link: {e}", file=sys.stderr)
        return 2
    if argv[0] == "routes":
        if len(argv) != 2 or argv[1] not in conns:
            print(f"lmost link routes <connection> — there are: {', '.join(conns) or 'none'}", file=sys.stderr)
            return 2
        x = link.Link(conns[argv[1]])
        print(f"# {argv[1]}: {x.routes_src or 'default set'}")
        for r in x.routes:
            print(r["line"] + (f"   # off: {r['off']}" if r.get("off") else ""))
        return 0
    return link.serve(once=argv[0] == "once")
