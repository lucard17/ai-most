"""Import a setup of the private predecessor of ai-most (``chat-*`` tools) into the ai-most layout (``aimost migrate``).

Source layout (read only, never changed): config ``~/.config/chat`` (connections, bot tokens, default, confirm and
private patterns, peer-readonly), wake list ``~/.config/chat-inbox/wake.list``, state ``~/.local/state/chat-*``.
The file formats are the same; only places change:

    chat/connections/*          → <config>/connections/
    chat/bots/<conn>/<role>     → <config>/bots/<conn>/<role>
    chat/{default,confirm-patterns,confirm-deny,private-patterns,peer-readonly} → <config>/
    chat-inbox/wake.list        → <config>/wake.list
    state chat-inbox            → <state>/inbox      (events, cursors, modes, mutes, follows)
    state chat-confirm          → <state>/marks      (turn marks, confirmation records)
    state chat-activity         → <state>/activity   (without avatars/)
    state chat-link             → <state>/link       (snoozes, discussions, cursors)
    state chat-asks             → <state>/asks       (question registry)
    state chat-mr-watch         → <state>/gitlab     (merge requests linked to sessions — the same format)

A destination file that exists is kept (reported), never overwritten; ``*.lock`` and ``*.tmp`` are skipped. Copies are
0600 in 0700 directories. Connection keys with no meaning here are reported (``DROPPED_KEYS``). Dry run by default.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from . import connections, paths

CONFIG_FILES = ("default", "confirm-patterns", "confirm-deny", "private-patterns", "peer-readonly")
STATE_DIRS = (("chat-inbox", "inbox"), ("chat-confirm", "marks"), ("chat-activity", "activity"),
              ("chat-link", "link"), ("chat-asks", "asks"), ("chat-mr-watch", "gitlab"))
SKIP_DIRS = {"avatars", "__pycache__"}
# connection keys of the predecessor that ai-most does not read, and why
DROPPED_KEYS = {
    "PLAN": "answers into a task tracker — an answer-sink plugin now",
    "REVIEW_SESSIONS": "review admission — an admission plugin now",
}
NOT_MIGRATED: dict = {}   # state of the predecessor that is not imported, and why


class Item:
    def __init__(self, src: Path, dst: Path):
        self.src, self.dst = src, dst

    @property
    def kept(self) -> bool:
        return self.dst.exists()


def _files(d: Path) -> Iterator[Path]:
    for root, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        for f in sorted(files):
            if not f.endswith((".lock", ".tmp")):
                yield Path(root) / f


def plan(old_config: Path, old_state: Path, old_wake: Optional[Path] = None) -> List[Item]:
    cfg, st = paths.config_dir(), paths.state_dir()
    out: List[Item] = []
    for sub in ("connections", "bots"):
        for f in _files(old_config / sub):
            out.append(Item(f, cfg / sub / f.relative_to(old_config / sub)))
    for name in CONFIG_FILES:
        if (old_config / name).is_file():
            out.append(Item(old_config / name, cfg / name))
    wake = old_wake or old_config.parent / "chat-inbox" / "wake.list"
    if wake.is_file():
        out.append(Item(wake, paths.wake_list()))
    for old, new in STATE_DIRS:
        for f in _files(old_state / old):
            out.append(Item(f, st / new / f.relative_to(old_state / old)))
    return out


def dropped_keys(old_config: Path) -> List[Tuple[str, str, str]]:
    """(connection, key, why) for keys ai-most does not read."""
    out = []
    for p in sorted((old_config / "connections").glob("*.env")):
        for k in connections.read_env(p):
            if k in DROPPED_KEYS:
                out.append((p.name[:-4], k, DROPPED_KEYS[k]))
    return out


def apply(items: List[Item]) -> int:
    """Copy what is not there yet; returns the number copied."""
    n = 0
    for it in items:
        if it.kept:
            continue
        d = it.dst.parent
        chain = []
        while not d.exists():
            chain.append(d)
            d = d.parent
        for x in reversed(chain):
            paths.private_dir(x)
        shutil.copy2(it.src, it.dst)
        os.chmod(it.dst, 0o600)
        n += 1
    return n
