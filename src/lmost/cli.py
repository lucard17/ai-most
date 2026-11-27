"""``lmost`` command line: one command, subcommands per area (decision D9).

A subcommand ``x-y`` lives in module ``lmost.cli_x_y`` with ``main(argv) -> int``; adding a command is adding a module.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from typing import List, Optional

from . import __version__


def commands() -> List[str]:
    import lmost
    return sorted(m.name[4:].replace("_", "-") for m in pkgutil.iter_modules(lmost.__path__) if m.name.startswith("cli_"))


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(f"lmost {__version__} — bridge between agent sessions and Mattermost\n\ncommands: {', '.join(commands())}")
        return 0 if argv else 2
    if argv[0] == "--version":
        print(__version__)
        return 0
    name = argv[0]
    if name not in commands():
        print(f"lmost: unknown command {name!r} (commands: {', '.join(commands())})", file=sys.stderr)
        return 2
    from . import plugins
    plugins.load()  # every command sees the same extension points (a broken plugin is reported and skipped)
    return importlib.import_module(f"lmost.cli_{name.replace('-', '_')}").main(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
