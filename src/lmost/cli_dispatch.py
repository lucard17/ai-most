"""``lmost dispatch once|serve`` — the wake dispatcher (spec: delivery). See ``lmost.dispatch``."""

from __future__ import annotations

import sys
from typing import List

from . import dispatch


def main(argv: List[str]) -> int:
    if argv == ["once"]:
        dispatch.pass_once()
        return 0
    if argv == ["serve"]:
        try:
            dispatch.serve()
        except KeyboardInterrupt:
            return 0
    print("usage: lmost dispatch once | serve\n\n" + (dispatch.__doc__ or ""), file=sys.stderr)
    return 0 if argv[:1] in (["-h"], ["--help"]) else 2
