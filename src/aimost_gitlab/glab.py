"""Calls to GitLab through the ``glab`` CLI (no tokens of our own)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Optional

from aimost.connections import Connection


class Gone(RuntimeError):
    """The MR is deleted or no longer accessible (404/403)."""


class NotFound(Gone):
    """404: no such object (for a lookup — not a failure of GitLab)."""


def binary() -> str:
    return os.environ.get("AIMOST_GLAB_BIN", "glab")


def _configured() -> bool:
    """glab has a config of this user (it was logged in at least once). Without one it would talk to gitlab.com
    anonymously — not what a team running its own GitLab wants, so the plugin stays silent."""
    d = os.environ.get("GLAB_CONFIG_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"), "glab-cli")
    return os.path.isfile(os.path.join(d, "config.yml"))


def available() -> bool:
    """``glab`` can be used: ``AIMOST_GLAB_BIN`` given explicitly, or ``glab`` on PATH with a user config."""
    if os.environ.get("AIMOST_GLAB_BIN"):
        return shutil.which(binary()) is not None
    return shutil.which(binary()) is not None and _configured()


def api(path: str, timeout: float = 60) -> object:
    # from a neutral directory: inside a repo glab would pick the project from it
    try:
        r = subprocess.run([binary(), "api", path], capture_output=True, text=True, timeout=timeout,
                           cwd=tempfile.gettempdir())
    except FileNotFoundError:
        raise RuntimeError("glab not found")
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if re.search(r"\b404\b", err):
            raise NotFound(err[:160])
        if re.search(r"\b403\b", err):
            raise Gone(err[:160])
        raise RuntimeError(f"glab api {path} — exit {r.returncode}: {err[:200]}")
    return json.loads(r.stdout or "null")


def group(conn: Optional[Connection] = None) -> str:
    return ((conn or {}).get("GITLAB_GROUP") or os.environ.get("AIMOST_GITLAB_GROUP") or "").strip("/")
