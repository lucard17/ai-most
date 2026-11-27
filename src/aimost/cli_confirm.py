"""``aimost confirm`` — ask the owner to confirm a dangerous action with a button (spec: authority)."""

from __future__ import annotations

import argparse
import sys
from typing import List

from . import confirm, inbox, mattermost


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(
        prog="aimost confirm",
        description="Post 'Confirm? <what> <details>' with ✅/❌ and wait. Exit 0 — confirmed, prints the id (then run "
                    "AIMOST_CONFIRM=<id> <command>); 1 — declined; 2 — expired, or --to outside the allowed list (no "
                    "post); 3 — the session may not post; 4 — chat server error.")
    p.add_argument("action", help="what I am doing")
    p.add_argument("details", nargs="?", default="", help="scope: hosts, version, window")
    p.add_argument("--via", default="", help="connection (default: the session's own)")
    p.add_argument("--channel", default="", help="channel name, or dm (default)")
    p.add_argument("--thread", default="", help="thread root post id")
    p.add_argument("--to", default="", help="logins to mention, comma-separated (only allowed confirmers)")
    p.add_argument("--timeout", type=float, default=confirm.TTL_S, help="seconds to wait (default 900)")
    a = p.parse_args(argv)
    role = inbox.session_name()
    if not role:
        print("aimost confirm: no session name (set AIMOST_ROLE or /rename the session)", file=sys.stderr)
        return 2
    try:
        cid = confirm.request(role, a.action, a.details, via=a.via, channel=a.channel, thread=a.thread,
                              to=a.to.split(",") if a.to else (), timeout=a.timeout,
                              log=lambda m: print(f"aimost confirm: {m}", file=sys.stderr))
    except confirm.ConfirmError as e:
        print(f"aimost confirm: {e}", file=sys.stderr)
        return e.code
    except (mattermost.HTTPError, OSError) as e:
        print(f"aimost confirm: chat server error: {e}", file=sys.stderr)
        return 4
    print(cid)
    return 0
