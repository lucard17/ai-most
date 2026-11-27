"""``lmost file [--via <conn>] <file_id> [<dest>]`` — download a chat attachment into a local file; prints the path.

Default destination: ``~/.cache/lmost/files/<id>-<name>`` (``LMOST_FILES_DIR``). The id comes from an inbox event
("📎 files (lmost file <id>): …"). Read with the role's own bot token.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List

from . import mattermost, post
from .cli_thread import reader


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="lmost file", description="Download a chat attachment.")
    ap.add_argument("--via", default="")
    ap.add_argument("file_id")
    ap.add_argument("dest", nargs="?")
    a = ap.parse_args(argv)
    try:
        p = reader(a.via)
        info = p.api("GET", f"/files/{a.file_id}/info")
        name = re.sub(r"[^\w.\-]+", "_", info.get("name") or a.file_id)
        base = Path(os.environ.get("LMOST_FILES_DIR") or Path(os.path.expanduser("~")) / ".cache" / "lmost" / "files")
        dest = Path(a.dest) if a.dest else base / f"{a.file_id}-{name}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not a.dest:
            os.chmod(dest.parent, 0o700)
        dest.write_bytes(mattermost.download(p.conn, a.file_id, p.token))
    except post.PostError as e:
        print(f"lmost file: {e}", file=sys.stderr)
        return e.code
    except Exception as e:
        print(f"lmost file: {e}", file=sys.stderr)
        return 1
    print(dest)
    return 0
