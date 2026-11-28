"""Integration world from tests/integration/it.env (written by tests/integration/up.sh). Tests that need a real
Mattermost skip when it is not running: the fast suite never needs Docker."""

from __future__ import annotations

import os as _os

# the props prefix is read once, at import: the machine's own (~/.config/aimost/props-prefix, e.g. `chat` after a team
# switch) must not leak into the tests — they run with the default unless the run sets AIMOST_PROPS_PREFIX itself
_os.environ.setdefault("AIMOST_PROPS_PREFIX", "aimost")

import os
import unittest
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / "integration" / "it.env"


def load() -> dict:
    try:
        lines = ENV_FILE.read_text().splitlines()
    except FileNotFoundError:
        return {}
    return dict(line.split("=", 1) for line in lines if "=" in line)


IT = load()
requires_mattermost = unittest.skipUnless(
    IT.get("AIMOST_IT_URL") and os.environ.get("AIMOST_IT", "1") != "0",
    "no disposable Mattermost (run tests/integration/up.sh)")
