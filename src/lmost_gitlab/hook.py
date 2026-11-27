"""PostToolUse hook: a session created an MR → link it (gitlab-mr spec, "An MR is linked to the session that created it").

An MR-creating command is ``glab mr create`` or a command word from ``MR_CREATE_PATTERNS`` (the role's connection, or
``LMOST_MR_CREATE_PATTERNS``; comma-separated words, e.g. a wrapper script), in command position. Success is an MR URL in
the tool output, not the exit code. A subagent's MR goes to its parent session (the hook input carries the parent's
session id). The network part (``add`` takes a snapshot) runs in a detached process; the hook prints nothing and never
blocks. No inbox directory (no chat on this machine), no ``glab`` or the plugin switched off — exit at once.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import List, Optional

from lmost import connections, inbox, paths

from . import glab, watch

URL = re.compile(r"https?://[^\s)>\"']+/-/merge_requests/\d+")
CMDPOS = r"(?:^|[;&|(]|\$\()\s*(?:(?:cd|pushd)\s[^;&|]*(?:&&|;)\s*)?(?:[A-Za-z_][A-Za-z_0-9]*=\S*\s+)*"


def patterns(conn: Optional[dict]) -> List[str]:
    raw = ((conn or {}).get("MR_CREATE_PATTERNS") or os.environ.get("LMOST_MR_CREATE_PATTERNS") or "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def creates_mr(cmd: str, words: List[str]) -> bool:
    if re.search(CMDPOS + r"glab\s+mr\s+create(?:\s|$)", cmd):
        return True
    return any(re.search(CMDPOS + r"(?:[^\s;&|\"']*/)?" + re.escape(w) + r"(?:\s|$)", cmd) for w in words)


def mr_url(resp: object) -> Optional[str]:
    if isinstance(resp, dict):
        text = " ".join(str(resp.get(k) or "") for k in ("stdout", "stderr", "output"))
    else:
        text = str(resp or "")
    m = URL.search(text)
    return m.group(0) if m else None


def handle(raw: str, spawn=None) -> Optional[str]:
    """Returns the linked URL (spawned in the background), or None."""
    if not paths.inbox_dir().is_dir() or not glab.available():
        return None
    try:
        inp = json.loads(raw)
    except ValueError:
        return None
    cmd = ((inp.get("tool_input") or {}).get("command") or "")
    sid = inp.get("session_id") or ""
    if not cmd or not sid:
        return None
    name = inbox.session_name(sid)
    if not name or not watch.switched_on(name):
        return None
    if not creates_mr(cmd, patterns(connections.own_connection(name))):
        return None
    url = mr_url(inp.get("tool_response"))
    if not url:
        return None
    (spawn or _spawn)(["gitlab", "add", url, "--session", name])
    return url


def _spawn(args: List[str]) -> None:
    try:
        subprocess.Popen([sys.executable, "-m", "lmost", *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         env=dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p)))
    except OSError:
        pass
