"""``aimost plugins`` — plugins found by the core: name, kinds, version, source, state, and where each is enabled.

  aimost plugins          table
  aimost plugins --json   the same as JSON
"""

from __future__ import annotations

import json
from typing import List

from . import connections, plugins


def main(argv: List[str]) -> int:
    if any(a not in ("--json",) for a in argv):
        print(__doc__)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    conns = connections.connections()
    rows = []
    for p in plugins.load():
        r = p.row()
        r["enabled"] = ",".join(n for n, c in conns.items() if plugins.enabled(p.name, c)) if conns else (
            "yes" if plugins.enabled(p.name, None) else "no")
        rows.append(r)
    if "--json" in argv:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    cols = ("name", "kinds", "version", "source", "state", "enabled")
    w = {c: max([len(c)] + [len(r[c]) for r in rows]) for c in cols}
    print("  ".join(c.ljust(w[c]) for c in cols).rstrip())
    for r in rows:
        print("  ".join(r[c].ljust(w[c]) for c in cols).rstrip())
    return 0
