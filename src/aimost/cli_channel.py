"""``aimost channel`` — a channel's purpose and header, set by the role's bot (spec: text).

  aimost channel show <channel> [--via <conn>]                              purpose, header, pins (``aimost_pin`` keys)
  aimost channel set <channel> [--purpose "<phrase>"] [--header "<line>"] [--via <conn>]

Layout of a channel: purpose — one phrase (≤250 characters, shown in the channel list); header — one line with links
(≤1024); rules — a pin by key (``aimost post --channel <channel> --pin rules``). Text checks as for a post (private guard,
logins). The role's bot must be in the channel.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
from typing import List

from . import post, textcheck
from .cli_thread import reader


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="aimost channel", description="Channel purpose and header.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("show", "set"):
        s = sub.add_parser(n)
        s.add_argument("channel")
        s.add_argument("--via", default="")
        if n == "set":
            s.add_argument("--purpose")
            s.add_argument("--header")
    a = ap.parse_args(argv)
    try:
        p = reader(a.via)
        try:
            ch = p.api("GET", f"/teams/name/{p.conn.get('TEAM', '')}/channels/name/{a.channel}")
        except urllib.error.HTTPError as x:
            if x.code not in (403, 404):
                raise
            raise post.PostError(f"no channel {a.channel!r} visible to bot {p.role}")
        if a.cmd == "show":
            pinned = (p.api("GET", f"/channels/{ch['id']}/pinned") or {}).get("posts") or {}
            print(f"purpose: {ch.get('purpose') or '—'}\nheader: {ch.get('header') or '—'}")
            for x in sorted(pinned.values(), key=lambda x: x.get("create_at", 0)):
                k = (x.get("props") or {}).get("aimost_pin")
                first = (x.get("message") or "").splitlines()[0][:80] if x.get("message") else ""
                print(f"pin {x['id']}{' [' + str(k) + ']' if k else ''}: {first}")
            return 0
        patch = {k: getattr(a, k) for k in ("purpose", "header") if getattr(a, k) is not None}
        if not patch:
            raise post.PostError("nothing to change: --purpose and/or --header")
        if len(patch.get("purpose", "")) > 250:
            raise post.PostError("purpose longer than 250 characters")
        if len(patch.get("header", "")) > 1024:
            raise post.PostError("header longer than 1024 characters")
        p.check(textcheck.Draft("", extra=list(patch.values()), plain=True))
        p.api("PUT", f"/channels/{ch['id']}/patch", patch)
        print(f"channel {a.channel}: " + ", ".join(f"{k} updated" for k in patch))
        return 0
    except post.PostError as e:
        print(f"aimost channel: {e}", file=sys.stderr)
        return e.code
    except urllib.error.HTTPError as e:
        print(f"aimost channel: chat server: HTTP {e.code} {e.reason}", file=sys.stderr)
        return 3 if e.code == 403 else 1
