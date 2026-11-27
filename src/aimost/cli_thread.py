"""``aimost thread [--via <conn>] [--last N] <thread>`` — read a chat thread (the memory of a task lives in its thread).

N last posts (40 by default), a long post — cut. The thread id comes from an inbox event (``thread=…``). Read with the
role's own bot token: the role sees what its bot sees.
"""

from __future__ import annotations

import argparse
import datetime
import sys
from typing import Dict, List

from . import post


def reader(via: str):
    """(Poster) for reading as the role's own bot; the relay is not used for reading."""
    role = post.role_name()
    if not role:
        raise post.PostError("no role name (not in a Claude Code session and no AIMOST_ROLE)")
    p = post.Poster(role, via or None)
    if p.relay:
        raise post.PostError(f"{role} has no bot of its own in {p.conn['NAME']} (the relay does not read for sessions)", 3)
    return p


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="aimost thread", description="Read a chat thread as the role's bot.")
    ap.add_argument("--via", default="")
    ap.add_argument("--last", type=int, default=40)
    ap.add_argument("thread")
    a = ap.parse_args(argv)
    try:
        p = reader(a.via)
        r = p.api("GET", f"/posts/{a.thread}/thread")
    except post.PostError as e:
        print(f"aimost thread: {e}", file=sys.stderr)
        return e.code
    except Exception as e:
        print(f"aimost thread: {e}", file=sys.stderr)
        return 1
    posts = sorted(r["posts"].values(), key=lambda x: x["create_at"])
    names: Dict[str, str] = {}

    def name(uid: str) -> str:
        if uid not in names:
            try:
                names[uid] = p.api("GET", f"/users/{uid}").get("username", uid[:6])
            except Exception:
                names[uid] = uid[:6]
        return names[uid]

    skipped = max(0, len(posts) - a.last)
    if skipped:
        print(f"… {skipped} earlier posts skipped (--last {len(posts)} — all)")
    for x in posts[-a.last:] if a.last > 0 else []:
        if x.get("type"):
            continue
        t = datetime.datetime.fromtimestamp(x["create_at"] / 1000).strftime("%d.%m %H:%M")
        m = x.get("message") or ""
        m = m if len(m) <= 1500 else m[:1500] + f" … (+{len(m) - 1500})"
        print(f"· {t} @{name(x['user_id'])}: {m}")
    return 0
