"""``aimost inbox`` — session inbox commands (spec: delivery)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List

from . import inbox


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="aimost inbox", description="Session inbox: events for a role by its name.")
    sub = p.add_subparsers(dest="cmd", required=True)

    put = sub.add_parser("put", help="append an event")
    put.add_argument("role")
    put.add_argument("text")
    put.add_argument("--from", dest="sender", default="")
    put.add_argument("--kind", default="message")
    put.add_argument("--via", default="", help="chat connection the event came from; the reply goes there")
    put.add_argument("--role", dest="sender_role", default="", choices=["", "owner", "peer", "person"])
    put.add_argument("--channel", default="")
    put.add_argument("--thread", default="")
    put.add_argument("--post", default="", help="chat post id; on --ack the role's bot marks it 👀")

    un = sub.add_parser("unread", help="unread events")
    un.add_argument("role")
    un.add_argument("--ack", action="store_true", help="move the cursor past them")
    un.add_argument("--before-peer", action="store_true", help="only events before the first peer post")

    for name, helptext in (("count", "number of unread events"), ("urgent", "exit 0 if an urgent event is unread"),
                           ("peer-pending", "unread peer posts: '<N> <senders>'; none — exit 1")):
        sub.add_parser(name, help=helptext).add_argument("role")

    show = sub.add_parser("show", help="one event in full (number from unread)")
    show.add_argument("role")
    show.add_argument("number", type=int)

    for name in ("mute", "unmute", "follow", "unfollow"):
        s = sub.add_parser(name, help="thread subscription")
        s.add_argument("role")
        s.add_argument("thread")

    nm = sub.add_parser("name", help="role name of a session id")
    nm.add_argument("session_id", nargs="?", default="")

    md = sub.add_parser("mode", help="session mode: chat|cli")
    md.add_argument("role")
    md.add_argument("value", nargs="?", choices=["chat", "cli", "auto"])
    md.add_argument("--if-unset", action="store_true")

    a = p.parse_args(argv)
    try:
        if a.cmd == "name":
            n = inbox.session_name(a.session_id)
            if not n:
                return 1
            print(n)
            return 0
        if a.cmd == "mode":
            if a.value:
                inbox.set_mode(a.role, a.value, if_unset=a.if_unset)
            m, src = inbox.mode_of(a.role)
            print(f"{m} ({src})")
            return 0
        box = inbox.Inbox(a.role)
        if a.cmd == "put":
            box.put(a.text, sender=a.sender, kind=a.kind, via=a.via, role=a.sender_role, channel=a.channel,
                    thread=a.thread, post=a.post)
            if inbox.mode_of(a.role)[0] == "cli":
                print(f"aimost inbox: {a.role} is in CLI mode — it answers when its owner runs /inbox (event stored)",
                      file=sys.stderr)
            return 0
        if a.cmd == "unread":
            events = box.unread(ack=a.ack, before_peer=a.before_peer)
            known = inbox.handles() if events else {}
            limit = int(os.environ.get("AIMOST_EVENT_MAX", inbox.DEFAULT_EVENT_MAX))
            for i, e in events:
                print(inbox.format_event(i, e, a.role, known, limit))
            return 0
        if a.cmd == "count":
            print(box.count())
            return 0
        if a.cmd == "urgent":
            return 0 if box.urgent() else 1
        if a.cmd == "peer-pending":
            r = box.peer_pending()
            if not r:
                return 1
            print(f"{r[0]} {', '.join(r[1])}")
            return 0
        if a.cmd == "show":
            e = box.show(a.number)
            if e is None:
                return 1
            print(json.dumps(e, ensure_ascii=False, indent=2))
            return 0
        box.subscribe(a.cmd, a.thread)
        print(f"{a.role}: {a.cmd} {a.thread}")
        return 0
    except inbox.InboxError as err:
        print(f"aimost inbox: {err}", file=sys.stderr)
        return err.code
