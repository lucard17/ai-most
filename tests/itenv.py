"""Integration world from tests/integration/it.env (written by tests/integration/up.sh). Tests that need a real
Mattermost skip when it is not running: the fast suite never needs Docker."""

from __future__ import annotations

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
    IT.get("LMOST_IT_URL") and os.environ.get("LMOST_IT", "1") != "0",
    "no disposable Mattermost (run tests/integration/up.sh)")
