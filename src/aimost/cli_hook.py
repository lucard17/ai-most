"""``aimost hook inbox|ask-guard|confirm-guard|peer-guard`` — hook bodies for Claude Code and Codex: the hook JSON on
stdin, the answer on stdout (and stderr with exit code 2 for a Codex refusal). See ``aimost.hooks``."""

from __future__ import annotations

import sys
from typing import List

from . import hooks


def main(argv: List[str]) -> int:
    if len(argv) != 1 or argv[0] not in hooks.HOOKS:
        print(f"usage: aimost hook {'|'.join(hooks.HOOKS)} < hook-input.json", file=sys.stderr)
        return 2 if argv[:1] not in (["-h"], ["--help"]) else 0
    try:
        r = hooks.run(argv[0], sys.stdin.read())
    except Exception as e:
        # Delivery fails open (a broken inbox hook must not stop the session); a guard fails closed — exit 2 blocks the
        # tool call in Claude Code and Codex alike, with the error as the reason.
        print(f"aimost hook {argv[0]}: {type(e).__name__}: {e}", file=sys.stderr)
        return 0 if argv[0] == "inbox" else 2
    if r.stdout:
        sys.stdout.write(r.stdout if r.stdout.endswith("\n") else r.stdout + "\n")
    if r.stderr:
        sys.stderr.write(r.stderr)
    return r.code
