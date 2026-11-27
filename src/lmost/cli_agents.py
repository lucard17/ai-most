"""``lmost agents`` — the agent directory built from bot profiles.

  lmost agents table   [--via C]                 the directory as a markdown table on stdout
  lmost agents publish [--via C] [--channel CH]  pinned directory post in a channel (default town-square): edited in
                                                 place, created and pinned if missing; posts with the session's bot

Exit codes: 2 usage / no connection, 3 the session has no bot, 5 private text in a bot description (not published).
"""

from __future__ import annotations

import re
import sys
from typing import List

from . import agents, connections, inbox, mattermost, textcheck


def _err(msg: str, code: int) -> int:
    print("lmost agents: " + msg, file=sys.stderr)
    return code


def private_hits(text: str) -> List[str]:
    """Private patterns found in the text; raises OSError when the patterns file is missing."""
    return [p for p in textcheck.private_patterns() if re.search(p, text, re.I)]


def main(argv: List[str]) -> int:
    if not argv or argv[0] not in ("table", "publish") or any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    opts = {"via": connections.default_name() or "", "channel": "town-square"}
    while rest:
        if rest[0] in ("--via", "--channel") and len(rest) >= 2:
            opts[rest[0][2:]] = rest[1]
            rest = rest[2:]
        else:
            return _err(f"unknown argument {rest[0]!r}", 2)
    conn = connections.connections().get(opts["via"])
    if not conn:
        return _err(f"no connection {opts['via']!r}", 2)
    role = inbox.session_name() or ""
    tok = connections.bot(conn, role) if role else None
    if not tok:
        return _err(f"session {role or '?'!r} has no bot in {conn['NAME']}", 3)

    def api(method: str, path: str, body: object = None) -> object:
        return mattermost.api(conn, method, path, body, token=tok)

    text = agents.table(conn, agents.bots(conn, api))
    if cmd == "table":
        print(text)
        return 0
    if conn.get("PRIVATE_GUARD") == "on":
        try:
            hits = private_hits(text)
        except OSError:
            return _err("PRIVATE_GUARD=on but no private-patterns file — directory not published", 5)
        if hits:
            return _err(f"private text in bot descriptions ({', '.join(hits)}) — fix the description; not published", 5)
    outcome, pid = agents.publish(conn, api, opts["channel"], text)
    print(f"directory {outcome} (post {pid})")
    return 0
