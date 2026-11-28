"""``aimost admin`` — ask for an admin action in the chat (the optional ``admin`` plugin, admin spec).

  aimost admin request --spec <request.json>|- [--channel <name>] [--thread <root id>] [--dry] [--via <conn>]
      request: {"action": "...", "target": {...}, "why": "...", "origin": "<who asked first>"}
      --dry — check the form only and print the normalized request (does not go to the chat)
  aimost admin actions     the actions and their fields

Exit codes: 0 — the request is posted (or --dry: the form is right), 2 — wrong form / no connection or bot, 3 — the chat refused.
"""

from __future__ import annotations

import json
import sys
from typing import List

from . import connections


def main(argv: List[str]) -> int:
    from aimost_admin import executor, request
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0 if argv else 2
    if argv[0] == "actions":
        print(executor.ACTIONS_HELP)
        return 0
    if argv[0] != "request":
        print(f"aimost admin: unknown {argv[0]!r} — request | actions", file=sys.stderr)
        return 2
    opts, dry, a = {}, False, argv[1:]
    while a:
        k = a.pop(0)
        if k in ("--spec", "--channel", "--thread", "--via") and a:
            opts[k[2:]] = a.pop(0)
        elif k == "--dry":
            dry = True
        else:
            print(f"aimost admin: unknown {k!r}", file=sys.stderr)
            return 2
    if not opts.get("spec"):
        print("aimost admin: --spec <request.json> is needed", file=sys.stderr)
        return 2
    try:
        req = json.load(sys.stdin if opts["spec"] == "-" else open(opts["spec"], encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"aimost admin: the request does not read: {e}", file=sys.stderr)
        return 2
    try:
        if dry:
            print(json.dumps(request.normalize(req), ensure_ascii=False, indent=1))
            return 0
        via = opts.get("via") or connections.default_name() or ""
        conn = connections.connections().get(via)
        if not conn:
            raise request.RequestError(f"no connection {via!r}")
        print(request.send(conn, req, opts.get("channel", ""), opts.get("thread", "")))
        return 0
    except request.RequestError as e:
        print(f"aimost admin: {e}", file=sys.stderr)
        return e.code
