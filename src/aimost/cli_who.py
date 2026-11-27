"""``aimost who <name | chat login> [--via <conn>]`` — find a person's chat login (logins differ between systems).

Searches the chat (``/users/search`` by login, first and last name, nickname) for the whole term and its pieces, then
asks the directories registered in ``WHO_SOURCES`` (a plugin may add e.g. a code-review system or a team map). Logins
those directories return are checked in the chat by exact match. Read-only.

A directory: ``fn(term) -> list of {"source", "login", "name", "chat"}`` (``chat`` — the chat login if the directory knows
it); it may raise — the lookup goes on without it.
"""

from __future__ import annotations

import re
import sys
from typing import Callable, Dict, List

from . import connections, mattermost, post

WHO_SOURCES: List[Callable[[str], List[Dict[str, str]]]] = []


def main(argv: List[str]) -> int:
    a = list(argv)
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if a else 2
    via = ""
    if "--via" in a:
        i = a.index("--via")
        via = a[i + 1] if i + 1 < len(a) else ""
        del a[i:i + 2]
    term = " ".join(a).strip().lstrip("@")
    role = post.role_name() or ""
    conn = connections.connections().get(via) if via else (connections.own_connection(role) if role else None)
    if not conn:
        print(f"aimost who: no connection {via or '(the role has none; give --via)'}", file=sys.stderr)
        return 2
    tok = (connections.bot(conn, role) if role else None) or conn.get("BOT_TOKEN")
    if not tok:
        print(f"aimost who: no bot of {role or '?'} in {conn['NAME']} (and no BOT_TOKEN)", file=sys.stderr)
        return 3

    def call(method, path, body=None):
        return mattermost.api(conn, method, path, body, token=tok)

    pieces = [t for t in dict.fromkeys([term] + re.split(r"[\s._-]+", term)) if len(t) >= 2]
    found: Dict[str, dict] = {}
    for t in pieces:
        try:
            for u in call("POST", "/users/search", {"term": t, "allow_inactive": False}) or []:
                found[u["id"]] = u
        except Exception as x:
            print(f"aimost who: chat did not answer: {x}", file=sys.stderr)
            break
    other: List[Dict[str, str]] = []
    for fn in WHO_SOURCES:
        try:
            other += list(fn(term) or [])
        except Exception as x:
            print(f"aimost who: directory {getattr(fn, '__name__', fn)} failed: {x}", file=sys.stderr)
    known = {u["username"] for u in found.values()}
    look = sorted({h.get("chat") or h.get("login") for h in other if (h.get("chat") or h.get("login"))} - known)
    if look:
        try:
            for u in call("POST", "/users/usernames", look) or []:
                if not u.get("delete_at"):
                    u["_via"] = "login from a directory"
                    found[u["id"]] = u
        except Exception as x:
            print(f"aimost who: chat did not answer on {', '.join(look)}: {x}", file=sys.stderr)
    print(f"chat ({conn['NAME']}):")
    for u in found.values():
        nm = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
        print(f"  @{u['username']:<24} {nm}" + (f" ({u['nickname']})" if u.get("nickname") else "")
              + (" [bot]" if u.get("is_bot") else "") + (f" [{u['_via']}]" if u.get("_via") else ""))
    if not found:
        print("  —")
    for h in other:
        print(f"{h.get('source', 'directory')}: {h.get('login', '?')} {h.get('name', '')}"
              + (f" → chat @{h['chat']}" if h.get("chat") else ""))
    return 0
