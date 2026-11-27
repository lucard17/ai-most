"""The per-connection context the bridge and question upkeep share: the connection's bots (admitted roles and the relay),
their user ids and usernames, the owner's id, the co-owners of a role, a user cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from . import connections, mattermost, paths


class Ctx:
    """The connection's bots (admitted roles and the relay), their user ids, the owner's id, small caches."""

    def __init__(self, conn: dict):
        self.c, self.name = conn, conn["NAME"]
        relay = conn.get("RELAY", "").strip()
        self.bots = {n: t for n, t in connections.bots(conn).items() if n == relay or connections.admitted(conn, n)}
        self.ids: Dict[str, str] = {}
        self.handle: Dict[str, str] = {}      # role → its bot's username (a role ``hash`` may post as ``@me-hash``)
        for n, t in self.bots.items():
            me = mattermost.api(conn, "GET", "/users/me", token=t)
            self.ids[n], self.handle[n] = me["id"], me.get("username") or n
        self.users: Dict[str, dict] = {}
        self.owner = self.user_by_name(conn["OWNER"])["id"] if self.bots and conn.get("OWNER") else None
        self._owner_ids: Dict[str, set] = {}

    def api(self, method: str, path: str, body: object = None, who: Optional[str] = None):
        tok = self.bots[who] if who else next(iter(self.bots.values()))
        return mattermost.api(self.c, method, path, body, token=tok)

    def user(self, uid: str) -> dict:
        if uid not in self.users:
            self.users[uid] = self.api("GET", f"/users/{uid}")
        return self.users[uid]

    def user_by_name(self, login: str) -> dict:
        return self.api("GET", f"/users/username/{login}")

    def ids_of(self, logins) -> set:
        out = set()
        for login in logins or []:
            try:
                out.add(self.user_by_name(login)["id"])
            except Exception:
                pass
        return out

    def owner_ids(self, role: str) -> set:
        """The owner and the role's co-owners (CO_OWNERS) — chat ids."""
        if role not in self._owner_ids:
            self._owner_ids[role] = {self.owner} | self.ids_of(x for x in connections.owners(self.c, role)
                                                                if x != self.c.get("OWNER"))
        return self._owner_ids[role]

    def state(self, suffix: str) -> Path:
        return paths.link_state_dir() / f"{self.name}.{suffix}.json"

    def link(self, post_id: str) -> str:
        return self.c.get("URL", "").rstrip("/") + "/" + self.c.get("TEAM", "") + "/pl/" + str(post_id)


_ctx: Dict[str, Ctx] = {}


def context(conn: dict) -> Ctx:
    """Cached per connection; rebuilt when its bots change (``lmost bot add``)."""
    key = conn["NAME"]
    c = _ctx.get(key)
    if c is None or c.c != conn or set(c.bots) != {n for n in connections.bots(conn)
                                                   if n == conn.get("RELAY", "").strip() or connections.admitted(conn, n)}:
        c = _ctx[key] = Ctx(conn)
    return c
