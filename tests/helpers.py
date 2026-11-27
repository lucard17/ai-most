"""Shared test setup: every test gets its own HOME and state directories, nothing touches the real ones."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parent.parent / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

ENV_KEYS = ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "AIMOST_CONFIG_DIR", "AIMOST_STATE_DIR", "AIMOST_INBOX_DIR",
            "AIMOST_MARKS_DIR", "AIMOST_LINK_STATE_DIR", "AIMOST_WAKE_LIST", "AIMOST_ROLE", "AIMOST_URGENT_KINDS",
            "AIMOST_EVENT_MAX", "AIMOST_PLUGINS", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR")


class IsolatedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HOME"] = str(self.tmp / "home")
        os.environ["AIMOST_STATE_DIR"] = str(self.tmp / "state")
        os.environ["AIMOST_CONFIG_DIR"] = str(self.tmp / "config")
        (self.tmp / "home").mkdir()

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def aimost(self, *args: str, env: dict = None) -> subprocess.CompletedProcess:
        e = dict(os.environ, PYTHONPATH=SRC, **(env or {}))
        return subprocess.run([sys.executable, "-m", "aimost", *args], capture_output=True, text=True, env=e)
