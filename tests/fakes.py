"""Fakes for hook, guard and dispatcher tests: Claude Code transcripts, a fake ``claude`` binary, extra env isolation."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from helpers import IsolatedTest

EXTRA_ENV = ("LMOST_CLAUDE_BIN", "LMOST_WAKE_MIN_S", "LMOST_WAKE_VERIFY_S", "LMOST_RESUME_RETRY_S", "LMOST_WAKE_ARGS",
             "LMOST_WAKE_SETTINGS", "LMOST_WAKE_NO_SCOPE", "LMOST_POST_BIN", "LMOST_BUSY_QUIET_S", "LMOST_ASKS_DIR",
             "LMOST_PEER_TMP", "LMOST_PEER_WRITABLE", "LMOST_CONFIRM_EVERY_S", "LMOST_WAKE_HINT",
             "CLAUDE_CODE_AGENT", "CLAUDE_CODE_SESSION_ATTENDED")


def iso(offset_s: float = 0) -> str:
    return datetime.fromtimestamp(time.time() + offset_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123Z")


class SessionTest(IsolatedTest):
    """IsolatedTest + the env keys hooks and the dispatcher read, + helpers to fake sessions."""

    def setUp(self) -> None:
        super().setUp()
        saved = {k: os.environ.pop(k, None) for k in EXTRA_ENV}

        def restore() -> None:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        self.projects = self.tmp / "home" / ".claude" / "projects" / "p"
        self.projects.mkdir(parents=True)

    def session(self, name: str, cwd: Optional[str] = None, sid: Optional[str] = None) -> str:
        """A transcript titled ``name``; returns the session id."""
        sid = sid or str(uuid.uuid4())
        recs = [{"type": "custom-title", "customTitle": name, "sessionId": sid}]
        if cwd:
            recs.append({"type": "user", "cwd": cwd})
        self.transcript(sid).write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs))
        return sid

    def transcript(self, sid: str) -> Path:
        return self.projects / f"{sid}.jsonl"

    def append(self, sid: str, *recs: dict) -> None:
        with open(self.transcript(sid), "a") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")

    @staticmethod
    def user(kind: str, text: str = "x", offset_s: float = 0, uid: str = "") -> dict:
        return {"type": "user", "uuid": uid or str(uuid.uuid4()), "timestamp": iso(offset_s), "origin": {"kind": kind},
                "message": {"role": "user", "content": text}}

    @staticmethod
    def queued(kind: str, offset_s: float = 0) -> dict:
        return {"type": "attachment", "timestamp": iso(offset_s),
                "attachment": {"type": "queued_command", "prompt": "do it", "origin": {"kind": kind}}}

    def fake_claude(self, agents: list = ()) -> Path:
        """A ``claude`` that answers ``agents --json`` from agents.json and logs every other call (args @cwd) to calls."""
        d = self.tmp / "claude"
        d.mkdir(exist_ok=True)
        exe = d / "claude"
        exe.write_text('#!/bin/sh\nd=$(dirname "$0")\nif [ "$1" = agents ]; then cat "$d/agents.json"; '
                       'else echo "$* @$PWD" >> "$d/calls"; fi\n')
        exe.chmod(0o755)
        self.set_agents(list(agents))
        os.environ["LMOST_CLAUDE_BIN"] = str(exe)
        return exe

    def set_agents(self, agents: list) -> None:
        (self.tmp / "claude" / "agents.json").write_text(json.dumps(agents))

    def calls(self) -> str:
        try:
            return (self.tmp / "claude" / "calls").read_text()
        except FileNotFoundError:
            return ""

    def clear_calls(self) -> None:
        (self.tmp / "claude" / "calls").write_text("")
