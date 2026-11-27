"""``lmost migrate`` — import a ``chat-*`` setup (the private predecessor) into the l-most layout. See ``lmost.migrate``.

  lmost migrate [--from-config DIR] [--from-state DIR] [--apply]

Defaults: ``~/.config/chat`` and ``~/.local/state``. Without ``--apply`` — only shows what would be copied. The source is
never changed; existing l-most files are kept. Afterwards: stop the old services and run ``lmost setup``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

from . import migrate, paths


def main(argv: List[str]) -> int:
    home = Path(os.path.expanduser("~"))
    o = {"from-config": str(home / ".config" / "chat"), "from-state": str(home / ".local" / "state")}
    do, rest = False, list(argv)
    while rest:
        a = rest.pop(0)
        if a == "--apply":
            do = True
        elif a in ("--from-config", "--from-state") and rest:
            o[a[2:]] = rest.pop(0)
        else:
            print(__doc__)
            return 0 if a in ("-h", "--help") else 2
    old_config, old_state = Path(o["from-config"]), Path(o["from-state"])
    if not old_config.is_dir() and not any((old_state / s).is_dir() for s, _ in migrate.STATE_DIRS):
        print(f"lmost migrate: nothing to import (no {old_config}, no {old_state}/chat-*)", file=sys.stderr)
        return 1
    items = migrate.plan(old_config, old_state)
    new = [i for i in items if not i.kept]
    for i in items:
        print(f"{'= kept' if i.kept else '+'} {i.dst}  ← {i.src}")
    for conn, key, why in migrate.dropped_keys(old_config):
        print(f"! {conn}: {key} is not read by l-most ({why})")
    for d, why in migrate.NOT_MIGRATED.items():
        if (old_state / d).is_dir():
            print(f"! {old_state / d} not imported: {why}")
    if not do:
        print(f"dry run: {len(new)} to copy, {len(items) - len(new)} kept — run with --apply")
        return 0
    n = migrate.apply(items)
    print(f"copied {n} into {paths.config_dir()} and {paths.state_dir()}; the old files are untouched")
    print("next: systemctl --user disable --now chat-link chat-dispatch chat-mr-watch.timer  (the old services), "
          "then lmost setup")
    return 0
