"""``lmost setup`` — prepare this machine and check the connections.

  lmost setup           config and state directories (0700), then the check
  lmost setup --check   per connection: URL and OWNER set, at least one bot, every bot token answers (/users/me),
                        OWNER exists on the server. Exit 0 — all fine, 1 — something to fix (listed)

Connections and bot tokens are written by hand (``<config>/connections/<name>.env``, ``<config>/bots/<name>/<role>``)
or by ``lmost bot add``; values come from the chat admin and never go into git. Background services (bridge, dispatcher)
are installed by the Claude Code plugin.
"""

from __future__ import annotations

import sys
from typing import List

from . import connections, mattermost, paths


def check(out=print) -> int:
    bad = 0
    conns = connections.connections()
    if not conns:
        out(f"  ✗ no connections: {paths.config_dir() / 'connections'}/<name>.env")
        return 1
    for name, c in conns.items():
        if not c.get("URL") or not c.get("OWNER"):
            out(f"  ✗ {name}: URL or OWNER missing")
            bad = 1
            continue
        bots = connections.bots(c)
        if not bots:
            out(f"  ✗ {name}: no bots ({paths.config_dir() / 'bots' / name}/<role>)")
            bad = 1
            continue
        good = ""
        for who, tok in bots.items():
            try:
                me = mattermost.api(c, "GET", "/users/me", token=tok)["username"]
                out(f"  ✓ {name}: {who} → @{me}")
                good = good or tok
            except Exception as e:
                out(f"  ✗ {name}: {who} — token does not answer ({e})")
                bad = 1
        if not good:
            continue
        try:
            mattermost.api(c, "GET", f"/users/username/{c['OWNER']}", token=good)
            out(f"  ✓ {name}: OWNER @{c['OWNER']} exists")
        except Exception as e:
            out(f"  ✗ {name}: OWNER @{c['OWNER']} not found ({e})")
            bad = 1
    return bad


def main(argv: List[str]) -> int:
    if any(a not in ("--check",) for a in argv):
        print(__doc__)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    if "--check" not in argv:
        for d in (paths.config_dir(), paths.config_dir() / "connections", paths.config_dir() / "bots",
                  paths.state_dir(), paths.inbox_dir()):
            paths.private_dir(d)
        print(f"+ {paths.config_dir()} and {paths.state_dir()} (0700)")
    print("check:")
    return check()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
