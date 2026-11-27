"""``lmost gitlab`` — the built-in GitLab plugin (gitlab-mr spec).

  lmost gitlab add <url | repo!N> --session <role> [--project <group/repo>]   link an MR to a session
  lmost gitlab rm <repo!N>                                                    unlink
  lmost gitlab ls                                                             links: MR → session
  lmost gitlab once                                                           one poll (timer)
  lmost gitlab hook                                                           PostToolUse hook (hook JSON on stdin)

Without ``glab`` ``once`` and ``hook`` do nothing and exit 0.
"""

from __future__ import annotations

import sys
from typing import List


def _err(msg: str, code: int = 2) -> int:
    print("lmost gitlab: " + msg, file=sys.stderr)
    return code


def main(argv: List[str]) -> int:
    from lmost_gitlab import glab, hook, watch
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "hook":
        try:
            hook.handle(sys.stdin.read())
        except Exception:  # a hook never breaks the tool call
            pass
        return 0
    if cmd == "once":
        if not glab.available():
            return 0
        return watch.once(lambda m: print("lmost gitlab: " + m, file=sys.stderr))
    if cmd == "ls":
        for ref, e in sorted(watch.links().items()):
            print(f"{ref}\t{e['session']}\t{(e.get('seen') or {}).get('state') or 'no snapshot yet'}")
        return 0
    if cmd == "rm" and len(rest) == 1:
        print(f"lmost gitlab: {rest[0]} " + ("unlinked" if watch.remove(rest[0]) else "was not linked"))
        return 0
    if cmd == "add" and rest:
        target, opts, i = rest[0], {"session": "", "project": ""}, 1
        while i < len(rest):
            if rest[i] in ("--session", "--project") and i + 1 < len(rest):
                opts[rest[i][2:]] = rest[i + 1]
                i += 2
            else:
                return _err(f"unknown argument {rest[i]!r}")
        if not opts["session"]:
            return _err("add needs --session <role>")
        try:
            ref = watch.add(target, opts["session"], opts["project"])
        except watch.WatchError as e:
            return _err(str(e), 1)
        print(f"lmost gitlab: {ref} → {opts['session']}")
        return 0
    print(__doc__)
    return 2
