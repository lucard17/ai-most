"""A small in-memory Mattermost for the loop arbiter and the admin executor: users and bots, teams, channels and members,
posts with threads and props, preferences, sidebar categories, bot tokens. Only the endpoints those modules call; every
call is logged as (method, path)."""

from __future__ import annotations

import io
import json
import re
import secrets
import time
import urllib.error
import urllib.parse


def err(path: str, code: int):
    return urllib.error.HTTPError(path, code, "fake", {}, io.BytesIO(b"{}"))


def nid() -> str:
    return secrets.token_hex(13)


class Chat:
    def __init__(self):
        self.users, self.bots, self.teams, self.channels, self.members = {}, {}, {}, {}, {}
        self.posts, self.prefs, self.cats, self.tokens, self.team_members = {}, {}, {}, {}, {}
        self.calls = []
        self.me = self.user("edge", bot=True)
        self.t = int(time.time() * 1000) - 50000   # ms, like Mattermost: the arbiter reads posts since "now - 60 s"

    # building the world
    def user(self, name, bot=False, owner=None, deleted=False):
        uid = nid()
        self.users[uid] = {"id": uid, "username": name, "is_bot": bot, "roles": "system_user"}
        if bot:
            self.bots[uid] = {"user_id": uid, "username": name, "owner_id": owner or "", "delete_at": 1 if deleted else 0}
        return uid

    def team(self, name):
        tid = nid()
        self.teams[tid] = {"id": tid, "name": name}
        return tid

    def channel(self, team, name, type_="O", members=()):
        cid = nid()
        self.channels[cid] = {"id": cid, "team_id": team, "name": name, "type": type_}
        self.members[cid] = set(members)
        return cid

    def post(self, channel, user, message="", root="", props=None, type_="", at=None, files=0):
        pid = nid()
        self.t = max(self.t + 1, int(time.time() * 1000))   # monotonic and real ms: marks carry real time
        at = self.t if at is None else at
        self.posts[pid] = {"id": pid, "channel_id": channel, "user_id": user, "message": message, "root_id": root,
                           "props": dict(props or {}), "type": type_, "create_at": at, "update_at": at,
                           "file_ids": ["f"] * files}
        return pid

    # the API
    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        p, _, q = path.partition("?")
        qs = urllib.parse.parse_qs(q)
        m = lambda rx: re.fullmatch(rx, p)  # noqa: E731
        I = r"([a-z0-9]{26})"
        if method == "GET" and p == "/users/me":
            return self.users[self.me]
        if method == "GET" and m(rf"/users/{I}"):
            u = self.users.get(m(rf"/users/{I}").group(1))
            if not u:
                raise err(path, 404)
            return u
        if method == "GET" and m(r"/users/username/([^/]+)"):
            n = m(r"/users/username/([^/]+)").group(1)
            for u in self.users.values():
                if u["username"] == n:
                    return u
            raise err(path, 404)
        if method == "GET" and p == "/bots":
            page = int((qs.get("page") or ["0"])[0])
            out = [b for b in self.bots.values() if not b["delete_at"]]
            return out[page * 200:(page + 1) * 200]
        if method == "GET" and m(rf"/bots/{I}"):
            b = self.bots.get(m(rf"/bots/{I}").group(1))
            if not b or (b["delete_at"] and "include_deleted" not in qs):
                raise err(path, 404)
            return b
        if method == "POST" and p == "/bots":
            uid = self.user(body["username"], bot=True)
            return self.bots[uid]
        if method == "POST" and m(rf"/bots/{I}/assign/{I}"):
            r = m(rf"/bots/{I}/assign/{I}")
            self.bots[r.group(1)]["owner_id"] = r.group(2)
            return self.bots[r.group(1)]
        if method == "POST" and m(rf"/bots/{I}/(enable|disable)"):
            r = m(rf"/bots/{I}/(enable|disable)")
            self.bots[r.group(1)]["delete_at"] = 0 if r.group(2) == "enable" else 1
            return self.bots[r.group(1)]
        if method == "GET" and m(rf"/users/{I}/teams"):
            uid = m(rf"/users/{I}/teams").group(1)
            return [t for tid, t in self.teams.items() if uid in self.team_members.get(tid, set())]
        if method == "GET" and m(rf"/users/{I}/teams/{I}/channels"):
            r = m(rf"/users/{I}/teams/{I}/channels")
            return [c for cid, c in self.channels.items() if c["team_id"] == r.group(2) and r.group(1) in self.members[cid]]
        if method == "POST" and m(rf"/teams/{I}/members"):
            self.team_members.setdefault(m(rf"/teams/{I}/members").group(1), set()).add(body["user_id"])
            return {}
        if method == "GET" and m(r"/teams/name/([^/]+)"):
            n = m(r"/teams/name/([^/]+)").group(1)
            for t in self.teams.values():
                if t["name"] == n:
                    return t
            raise err(path, 404)
        if method == "GET" and m(rf"/teams/{I}/channels/name/([^/]+)"):
            r = m(rf"/teams/{I}/channels/name/([^/]+)")
            for c in self.channels.values():
                if c["team_id"] == r.group(1) and c["name"] == r.group(2):
                    return c
            raise err(path, 404)
        if method == "GET" and m(rf"/channels/{I}"):
            c = self.channels.get(m(rf"/channels/{I}").group(1))
            if not c:
                raise err(path, 404)
            return c
        if method == "GET" and m(rf"/channels/{I}/posts"):
            cid = m(rf"/channels/{I}/posts").group(1)
            since = int((qs.get("since") or ["0"])[0])
            page = int((qs.get("page") or ["0"])[0])
            ps = sorted((x for x in self.posts.values() if x["channel_id"] == cid and
                         max(x["create_at"], x["update_at"]) > since), key=lambda x: -x["create_at"])
            if "page" in qs:
                ps = ps[page * 200:(page + 1) * 200]
            return {"order": [x["id"] for x in ps], "posts": {x["id"]: x for x in ps}}
        if method == "GET" and m(rf"/channels/{I}/members"):
            cid = m(rf"/channels/{I}/members").group(1)
            page = int((qs.get("page") or ["0"])[0])
            return [{"user_id": u} for u in sorted(self.members[cid])][page * 200:(page + 1) * 200]
        if m(rf"/channels/{I}/members/{I}"):
            r = m(rf"/channels/{I}/members/{I}")
            if method == "GET":
                if r.group(2) not in self.members[r.group(1)]:
                    raise err(path, 404)
                return {"user_id": r.group(2)}
            if method == "DELETE":
                self.members[r.group(1)].discard(r.group(2))
                return {}
        if method == "POST" and m(rf"/channels/{I}/members"):
            self.members[m(rf"/channels/{I}/members").group(1)].add(body["user_id"])
            return {}
        if method == "GET" and m(rf"/posts/{I}"):
            x = self.posts.get(m(rf"/posts/{I}").group(1))
            if not x:
                raise err(path, 404)
            return json.loads(json.dumps(x))
        if method == "GET" and m(rf"/posts/{I}/thread"):
            root = m(rf"/posts/{I}/thread").group(1)
            ps = {k: v for k, v in self.posts.items() if k == root or v["root_id"] == root}
            return {"order": sorted(ps, key=lambda k: ps[k]["create_at"]), "posts": json.loads(json.dumps(ps))}
        if method == "PUT" and m(rf"/posts/{I}/patch"):
            x = self.posts[m(rf"/posts/{I}/patch").group(1)]
            if "props" in body:
                x["props"] = dict(body["props"])
            if "message" in body:
                x["message"] = body["message"]
            self.t = max(self.t + 1, int(time.time() * 1000))
            x["update_at"] = self.t
            return x
        if method == "POST" and p == "/posts":
            pid = self.post(body["channel_id"], self.me, body.get("message", ""), body.get("root_id", ""), body.get("props"))
            return self.posts[pid]
        if method == "DELETE" and m(rf"/posts/{I}"):
            pid = m(rf"/posts/{I}").group(1)
            if pid not in self.posts:
                raise err(path, 404)
            for k in [k for k, v in self.posts.items() if k == pid or v["root_id"] == pid]:
                self.posts.pop(k)
            return {}
        if m(rf"/users/{I}/preferences/(.+)") and method == "GET":
            r = m(rf"/users/{I}/preferences/(.+)")
            rows = [{"name": n, "value": v} for (u, c, n), v in self.prefs.items() if u == r.group(1) and c == r.group(2)]
            if not rows:
                raise err(path, 404)
            return rows
        if method == "PUT" and m(rf"/users/{I}/preferences"):
            for x in body:
                self.prefs[(x["user_id"], x["category"], x["name"])] = x["value"]
            return {}
        if method == "POST" and m(rf"/users/{I}/preferences/delete"):
            for x in body:
                self.prefs.pop((x["user_id"], x["category"], x["name"]), None)
            return {}
        if m(rf"/users/{I}/teams/{I}/channels/categories(?:/{I})?"):
            r = m(rf"/users/{I}/teams/{I}/channels/categories(?:/{I})?")
            key = (r.group(1), r.group(2))
            cats = self.cats.setdefault(key, [])
            if method == "GET":
                return {"categories": json.loads(json.dumps(cats))}
            if method == "POST":
                c = dict(body, id=nid())
                cats.append(c)
                return c
            if method == "PUT":
                for i, c in enumerate(cats):
                    if c["id"] == r.group(3):
                        cats[i] = dict(body)
                return body
            if method == "DELETE":
                self.cats[key] = [c for c in cats if c["id"] != r.group(3)]
                return {}
        if method == "GET" and m(rf"/users/{I}/tokens"):
            uid = m(rf"/users/{I}/tokens").group(1)
            page = int((qs.get("page") or ["0"])[0])
            return [t for t in self.tokens.values() if t["user_id"] == uid and t["is_active"]][page * 200:(page + 1) * 200]
        if method == "POST" and p == "/users/tokens/revoke":
            self.tokens[body["token_id"]]["is_active"] = False
            return {}
        raise err(path, 501)

    def token(self, uid):
        tid = nid()
        self.tokens[tid] = {"id": tid, "user_id": uid, "is_active": True}
        return tid

    def now(self):
        return time.time()
