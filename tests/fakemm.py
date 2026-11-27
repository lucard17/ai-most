"""In-memory Mattermost for offline edge tests: just the endpoints the edge calls, with a request log."""

from __future__ import annotations

import re
import urllib.error
from typing import Dict, List, Optional


def _err(path: str, code: int) -> urllib.error.HTTPError:
    e = urllib.error.HTTPError(path, code, "fake", {}, None)
    try:
        e.close()                # nothing to read; closed now so a caught error leaves no open buffer behind
    except KeyError:             # Python < 3.9: an HTTPError without a body has nothing to close
        pass
    return e


def _nf(path: str) -> urllib.error.HTTPError:
    return _err(path, 404)


class FakeMM:
    def __init__(self) -> None:
        self.users: Dict[str, dict] = {}
        self.bots: Dict[str, Optional[str]] = {}          # bot user id → owner id
        self.posts: Dict[str, dict] = {}
        self.prefs: Dict[tuple, Dict[str, str]] = {}       # (uid, category) → {name: value}
        self.threads: Dict[str, List[dict]] = {}           # uid → threads
        self.tokens: Dict[str, List[str]] = {}             # bot id → token ids
        self.channels: Dict[str, str] = {}
        self.log: List[tuple] = []
        self.fail: Dict[str, int] = {}                     # path regex → HTTP code to raise
        self.me = "uEDGE"
        self.user("uEDGE", "edge", bot=True)
        self._n = 0

    def user(self, uid: str, name: str, bot: bool = False, owner: Optional[str] = None, props: dict = None) -> dict:
        u = {"id": uid, "username": name, "is_bot": bot, "props": dict(props or {})}
        self.users[uid] = u
        if bot and uid != "uEDGE":
            self.bots[uid] = owner
        return u

    def post(self, pid: str, uid: str, props: dict = None, message: str = "") -> dict:
        p = {"id": pid, "user_id": uid, "channel_id": "C", "message": message, "props": dict(props or {})}
        self.posts[pid] = p
        return p

    def requests(self, method: str = None, path_re: str = "") -> List[tuple]:
        return [r for r in self.log if (method is None or r[0] == method) and re.search(path_re, r[1])]

    def call(self, method: str, path: str, body: object = None) -> object:
        self.log.append((method, path, body))
        for rx, code in self.fail.items():
            if re.search(rx, path):
                raise _err(path, code)
        p = path.split("?")[0]
        q = dict(x.split("=", 1) for x in path.split("?", 1)[1].split("&")) if "?" in path else {}
        if method == "GET":
            if p == "/users/me":
                return self.users[self.me]
            m = re.fullmatch(r"/users/username/(.+)", p)
            if m:
                u = next((x for x in self.users.values() if x["username"] == m.group(1)), None)
                if not u:
                    raise _nf(path)
                return u
            m = re.fullmatch(r"/users/(\w+)/preferences/(\w+)", p)
            if m:
                d = self.prefs.get((m.group(1), m.group(2)))
                if not d:
                    raise _nf(path)
                return [{"name": k, "value": v} for k, v in d.items()]
            m = re.fullmatch(r"/users/(\w+)/teams/\w+/threads", p)
            if m:
                return {"threads": self.threads.get(m.group(1), [])}
            m = re.fullmatch(r"/users/(\w+)/tokens", p)
            if m:
                ids = self.tokens.get(m.group(1), [])
                page, per = int(q.get("page", 0)), int(q.get("per_page", 200))
                return [{"id": i} for i in ids[page * per:(page + 1) * per]]
            m = re.fullmatch(r"/users/(\w+)", p)
            if m:
                if m.group(1) not in self.users:
                    raise _nf(path)
                return self.users[m.group(1)]
            if p == "/bots":
                page, per = int(q.get("page", 0)), int(q.get("per_page", 200))
                rows = [{"user_id": b, "owner_id": o} for b, o in self.bots.items()]
                return rows[page * per:(page + 1) * per]
            m = re.fullmatch(r"/bots/(\w+)", p)
            if m:
                if m.group(1) not in self.bots:
                    raise _nf(path)
                return {"user_id": m.group(1), "owner_id": self.bots[m.group(1)]}
            m = re.fullmatch(r"/posts/(\w+)", p)
            if m:
                if m.group(1) not in self.posts:
                    raise _nf(path)
                return self.posts[m.group(1)]
            if p == "/config/client":
                return {"SiteURL": "https://chat.example"}
            m = re.fullmatch(r"/teams/(\w+)", p)
            if m:
                return {"id": m.group(1), "name": "team-" + m.group(1)}
        if method == "PUT":
            m = re.fullmatch(r"/posts/(\w+)/patch", p)
            if m:
                post = self.posts[m.group(1)]
                if "props" in body:
                    post["props"] = dict(body["props"])        # replaced whole, as the real server does
                if "message" in body:
                    post["message"] = body["message"]
                return post
            m = re.fullmatch(r"/users/(\w+)/preferences", p)
            if m:
                for x in body:
                    self.prefs.setdefault((x["user_id"], x["category"]), {})[x["name"]] = x["value"]
                return {"status": "OK"}
        if method == "POST":
            m = re.fullmatch(r"/users/(\w+)/preferences/delete", p)
            if m:
                for x in body:
                    self.prefs.get((x["user_id"], x["category"]), {}).pop(x["name"], None)
                return {"status": "OK"}
            m = re.fullmatch(r"/users/(\w+)/tokens", p)
            if m:
                self.tokens.setdefault(m.group(1), []).append("TID")
                return {"id": "TID", "token": "TOKEN-VALUE"}
            if p == "/users/tokens/revoke":
                return {"status": "OK"}
            if p == "/users/ids":
                return [self.users[i] for i in body if i in self.users]
            if p == "/channels/direct":
                key = "dm-" + "-".join(sorted(body))
                self.channels[key] = key
                return {"id": key}
            if p == "/posts":
                self._n += 1
                pid = f"new{self._n}"
                self.posts[pid] = dict(body, id=pid, user_id=self.me)
                return self.posts[pid]
            if p == "/actions/dialogs/open":
                return {}
        if method == "DELETE":
            m = re.fullmatch(r"/users/(\w+)/teams/\w+/threads/(\w+)/following", p)
            if m:
                return {"status": "OK"}
        raise _nf(path)
