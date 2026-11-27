"""``aimost react <conn> <role> <post_id> <emoji>`` — a receipt reaction of the role's bot on a chat post.

📬 ``mailbox_with_mail`` — the bridge put the post into the inbox; 👀 ``eyes`` — the session read it; ⚠️ ``warning`` —
not delivered. No network, no bot, not admitted — exit 1 silently: a receipt must never break delivery.
"""

from __future__ import annotations

import sys
from typing import List

from . import connections, mattermost


def react(via: str, role: str, post_id: str, emoji: str) -> bool:
    try:
        c = connections.connections()[via]
        tok = connections.bot(c, role)
        if not tok or not connections.admitted(c, role):
            return False
        me = mattermost.api(c, "GET", "/users/me", token=tok)["id"]
        mattermost.api(c, "POST", "/reactions", {"user_id": me, "post_id": post_id, "emoji_name": emoji}, token=tok)
        return True
    except Exception:
        return False


def main(argv: List[str]) -> int:
    if len(argv) != 4 or any(x in ("-h", "--help") for x in argv):
        print(__doc__)
        return 0 if argv[:1] in (["-h"], ["--help"]) else 2
    return 0 if react(*argv) else 1
