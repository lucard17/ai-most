"""A post to people has structure, not a wall of text (text spec, the layout check, exit code 11).

Why. Mattermost renders markdown, and on a phone a solid paragraph is a screen of text with nothing to hold on to. The rule
"the point in the first line, fields in bold each on its own line, lists as lists" does not hold when it lives only in the
agents' instructions — so ``aimost post`` refuses a post that breaks it, with a hint.

What counts: the text without code blocks (```), quote lines (``>``) and link addresses. A structure line is a list item
(``- ``, ``* ``, ``1. ``), a field or a heading at the start of a line (``**…``, ``#``), a table row (``|``). Other
consecutive lines form a paragraph.
"""

from __future__ import annotations

import re
from typing import List, Optional

FIRST_MAX = 200     # the first line — the point in one sentence
PARA_MAX = 350      # about three lines on a phone
FLAT_MAX = 500      # longer than this — not without a single structure line

STRUCT = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|\*\*|#{1,6}\s|\|)")
TABLE_ROW = re.compile(r"^\s*\|")
MENTIONS_ONLY = re.compile(r"^\s*(?:@[\w.-]+[\s,]*)+$")
HINT = ("the form of a post: the first line — the point in one sentence; then fields in **bold:**, each on its own line; "
        "lists — as `- ` items; a paragraph — up to three lines. A quote or a log — --plain")


def _clean(text: str) -> List[str]:
    out, fence = [], False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence or line.lstrip().startswith(">"):
            continue
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)      # [text](address) → text
        line = re.sub(r"https?://\S+", "", line)
        out.append(line.rstrip())
    return out


def problem(text: str) -> Optional[str]:
    """Why the text is not in form — a line with the hint, or None."""
    lines = _clean(text)
    body = [x for x in lines if x.strip()]
    if not body:
        return None
    first = next((x for x in body if not MENTIONS_ONLY.match(x)), "")
    if len(first.strip()) > FIRST_MAX:
        return (f"the first line is {len(first.strip())} characters (limit {FIRST_MAX}): the point in one sentence, the "
                "details below. " + HINT)
    run: List[str] = []
    worst = 0
    for x in lines + [""]:
        if x.strip() and not STRUCT.match(x):
            run.append(x.strip())
            continue
        worst = max(worst, len(" ".join(run)))
        run = []
    # a field line and a list item too: "**What:** …; **not:** …" at 600. A table row is not: a cell does not break into
    # lines, several values in it go with <br>
    longest = max([len(x.strip()) for x in body if not TABLE_ROW.match(x)] or [0])
    if longest > PARA_MAX and longest >= worst:
        return f"a line of {longest} characters (limit {PARA_MAX}) — each field and item on its own line. " + HINT
    if worst > PARA_MAX:
        return f"a paragraph of {worst} characters (limit {PARA_MAX}) — split it. " + HINT
    total = len(" ".join(x.strip() for x in body))
    if total > FLAT_MAX and not any(STRUCT.match(x) for x in body):
        return f"a post of {total} characters without a single field, list or heading. " + HINT
    return None
