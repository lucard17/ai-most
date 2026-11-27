"""Names ai-most leaves in the chat server: post props and user preference categories (``<prefix>_answers``,
``<prefix>_unfollow``, …).

The prefix is ``aimost`` unless ``AIMOST_PROPS_PREFIX`` or the file ``<config>/props-prefix`` (one word; read by every
process on the owner's machine — the bridge, ``post``, hooks — without passing the environment around) says otherwise.
The bridge and the edge of one chat server must use the same value (the edge — ``AIMOST_PROPS_PREFIX`` in its
environment). It exists for one case: a server where an earlier version of the bridge left its posts under another
prefix; with the same prefix the new bridge keeps answering their buttons and reading their questions. Read once per
process: names end up in module constants.
"""

from __future__ import annotations

import os
import re

from . import paths


def _configured() -> str:
    v = os.environ.get("AIMOST_PROPS_PREFIX", "").strip()
    if v:
        return v
    try:
        return (paths.config_dir() / "props-prefix").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


PREFIX = _configured() or "aimost"
if not re.fullmatch(r"[a-z][a-z0-9]{0,15}", PREFIX):
    raise SystemExit(f"aimost: AIMOST_PROPS_PREFIX={PREFIX!r} — expected [a-z][a-z0-9]{{0,15}}")

# names that the earlier version spelled differently under its own prefix
_LEGACY = {("chat", "agents"): "chat_agents_catalog"}


def P(name: str) -> str:
    """The full name of a prop or preference category: ``P("answers")`` → ``aimost_answers``."""
    return _LEGACY.get((PREFIX, name)) or f"{PREFIX}_{name}"
