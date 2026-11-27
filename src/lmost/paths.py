"""Where l-most keeps config and state. Every location can be overridden by an environment variable (tests rely on it)."""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or os.path.join(os.path.expanduser("~"), default))


def config_dir() -> Path:
    return Path(os.environ.get("LMOST_CONFIG_DIR") or _xdg("XDG_CONFIG_HOME", ".config") / "lmost")


def state_dir() -> Path:
    return Path(os.environ.get("LMOST_STATE_DIR") or _xdg("XDG_STATE_HOME", ".local/state") / "lmost")


def inbox_dir() -> Path:
    return Path(os.environ.get("LMOST_INBOX_DIR") or state_dir() / "inbox")


def marks_dir() -> Path:
    """Turn marks read by the guard hooks: <role>.chat, <role>.peer, <role>.owner-words."""
    return Path(os.environ.get("LMOST_MARKS_DIR") or state_dir() / "marks")


def link_state_dir() -> Path:
    return Path(os.environ.get("LMOST_LINK_STATE_DIR") or state_dir() / "link")


def wake_list() -> Path:
    return Path(os.environ.get("LMOST_WAKE_LIST") or config_dir() / "wake.list")


def private_dir(p: Path) -> Path:
    """Create a directory readable only by this user (the trust boundary of inboxes and marks)."""
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p
