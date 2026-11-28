"""The admin executor in the edge (admin spec).

A role (its bot — ``EDGE_ADMIN_REQUESTER``) posts a request into the log channel (``EDGE_ADMIN_LOG_CHANNEL``, private) with
props ``<prefix>_admin`` {action, target, why, origin} and calls ``/<command> <post id>`` (``AIMOST_ADMIN_COMMAND``, default
``admin``). The edge makes a dry run with its own rights and posts into the log channel: the plan first, then the
fingerprint and who asks; the buttons are signed with ``EDGE_ADMIN_SECRET``, which exists only in the edge's environment.
Execution — only on a press by a user from ``EDGE_ADMIN_CONFIRMERS`` on THAT edge post (its fingerprint and nonce equal the
button's), within 15 minutes, once, with the same fingerprint on a second dry run. Backups, the journal, rollback data — in
``EDGE_ADMIN_STATE`` (``backup/``, ``undo/``, ``journal.jsonl``, ``used/``). No journal line — no execution.
``EDGE_ADMIN_CMD_TOKEN`` — the slash command's token Mattermost sends. Who may ask for what — ``EDGE_ADMIN_ORIGINS``
(``<action>=<origin>|<origin>;…``, ``*`` for every action); by default only the edge owner (``EDGE_OWNER``).

Posts are deleted permanently: the copy goes into the journal before the first DELETE, attachments' files are not copied
(said in the post), there is no rollback. Everything else rolls back for 24 h, once; never into a private channel.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import unicodedata
import urllib.error
import urllib.parse
from collections import Counter
from typing import Dict, List, Optional, Tuple

from aimost.names import P

TTL_S = 900
ROLLBACK_TTL_S = 24 * 3600
MAX_POSTS = 100
MAX_PEOPLE = 500
MAX_WHY = 200
ID = re.compile(r"[a-z0-9]{26}")
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
SYSTEM_TYPES = ("system_join_channel", "system_leave_channel", "system_add_to_channel", "system_remove_from_channel",
                "system_join_team", "system_leave_team", "system_add_to_team", "system_remove_from_team",
                "system_header_change", "system_displayname_change", "system_purpose_change")
ACTIONS = ("posts-delete", "system-messages-delete", "sidebar-category", "channel-member-add", "channel-member-remove",
           "bot-create", "bot-enable")
IRREVERSIBLE = ("posts-delete", "system-messages-delete")
CLOSED_HINT = "a private channel: people are added to private channels by their members — ask a member of that channel"
REQ, CONFIRM, RESULT = P("admin"), P("admin_confirm"), P("admin_result")

ACTIONS_HELP = """posts-delete            target {"post_ids": [id…≤100]} — permanent delete; a root takes its thread replies
system-messages-delete  target {"team", "channel", "since": ms, "until": ms, "types": [system_…]?} — permanent delete
sidebar-category        target {"team", "name", "channels": [name…], "users": [login…]?, "members_of": [channel…]?}
channel-member-add      target {"team", "channel", "user"} — public channels only
channel-member-remove   target {"team", "channel", "user"} — any channel except DMs
bot-create              target {"team", "username", "owner", "display_name"?, "description"?, "channels": [public…]?}
bot-enable              target {"username"} — enable a disabled bot of the edge owner; live tokens are revoked first"""


class Refused(Exception):
    """A refusal: nothing is done, only a journal line."""


# ── the request form (also used by ``aimost admin request`` on the owner's machine — one source of rules) ──────────────
def code(s, n=MAX_WHY) -> str:
    """The requester's text in a post — one line in `code`: no markup, mentions, links, line breaks, invisible chars."""
    s = "".join(ch for ch in str(s or "") if unicodedata.category(ch) != "Cf")
    s = re.sub(r"[\s\u00a0\u2028\u2029]+", " ", s).strip().replace("`", "'")
    return "`" + (s[:n] + "…" if len(s) > n else s or "—") + "`"


def _names(v, what, rx=NAME, limit=50):
    if not isinstance(v, list) or not v or len(v) > limit or not all(isinstance(x, str) and rx.fullmatch(x) for x in v):
        raise Refused(f"{what}: a list of names (1–{limit})")
    return sorted(set(v))


def _line(v, what, n):
    if (not isinstance(v, str) or not v.strip() or len(v) > n or re.search(r"[\x00-\x1f\x7f\u2028\u2029]", v)
            or any(unicodedata.category(ch) == "Cf" for ch in v)):
        raise Refused(f"{what} — one line up to {n} characters")
    return v.strip()


def parse_origins(value: str, owner: str) -> Dict[str, Tuple[str, ...]]:
    """``EDGE_ADMIN_ORIGINS`` → action → allowed origins; unset — the edge owner for every action."""
    out = {a: ((owner,) if owner else ()) for a in ACTIONS}
    for part in (value or "").split(";"):
        a, _, who = part.strip().partition("=")
        names = tuple(x.strip() for x in who.split("|") if x.strip())
        if not a or not names:
            continue
        for act in (ACTIONS if a.strip() == "*" else (a.strip(),)):
            if act in out:
                out[act] = names
    return out


def validate(req, origins: Optional[Dict[str, Tuple[str, ...]]] = None):
    """Check and normalize a request {action, target, why, origin} → (action, target); else Refused. ``origins`` None —
    the form only (the requester's side does not know the server's list)."""
    if not isinstance(req, dict):
        raise Refused("the request is not an object")
    a = req.get("action")
    if a not in ACTIONS:
        raise Refused(f"action {code(a, 40)} is not in the list")
    if not isinstance(req.get("origin"), str) or not NAME.fullmatch(req["origin"]):
        raise Refused("origin — who asked first, a login")
    if origins is not None and req["origin"] not in origins.get(a, ()):
        raise Refused(f"{a} is asked only by {', '.join(origins.get(a, ())) or 'nobody'}, not {code(req['origin'], 40)}")
    _line(req.get("why"), "why", MAX_WHY)
    t = req.get("target")
    if not isinstance(t, dict):
        raise Refused("target is not an object")

    def one(k):
        if not isinstance(t.get(k), str) or not NAME.fullmatch(t[k]):
            raise Refused(f"no {k}")
        return t[k]
    if a == "posts-delete":
        ids = t.get("post_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_POSTS or not all(isinstance(x, str) and ID.fullmatch(x) for x in ids):
            raise Refused(f"post_ids: post ids, 1 to {MAX_POSTS}")
        return a, {"post_ids": sorted(set(ids))}
    if a == "system-messages-delete":
        since, until = t.get("since"), t.get("until")
        if type(since) is not int or type(until) is not int or since >= until:
            raise Refused("since < until, ms")
        types = t.get("types") or list(SYSTEM_TYPES)
        if not isinstance(types, list) or any(x not in SYSTEM_TYPES for x in types):
            raise Refused("types — system types only")
        return a, {"team": one("team"), "channel": one("channel"), "since": since, "until": until, "types": sorted(set(types))}
    if a == "sidebar-category":
        name = _line(t.get("name"), "name — the category name,", 22)
        users = _names(t["users"], "users", limit=MAX_PEOPLE) if t.get("users") else []
        mof = _names(t["members_of"], "members_of") if t.get("members_of") else []
        if not users and not mof:
            raise Refused("for whom: users or members_of")
        return a, {"team": one("team"), "name": name, "channels": _names(t.get("channels"), "channels"), "users": users,
                   "members_of": mof}
    if a in ("channel-member-add", "channel-member-remove"):
        return a, {"team": one("team"), "channel": one("channel"), "user": one("user")}
    if a == "bot-create":
        u = one("username")
        d = t.get("description") or ""
        if not isinstance(d, str) or len(d) > 1024:
            raise Refused("description — a string up to 1024")
        ch = _names(t["channels"], "channels") if t.get("channels") else []
        return a, {"team": one("team"), "username": u, "display_name": _line(t.get("display_name") or u, "display_name", 64),
                   "description": d, "owner": one("owner"), "channels": ch}
    # bot-enable: the owner is never taken from the request — "own" means a bot of the edge owner
    return a, {"username": one("username")}


# ── the executor ─────────────────────────────────────────────────────────────────────────────────────────────────────
_I = r"[a-z0-9]{26}"
_CAT = ("sidebar-category", "rollback:sidebar-category")
_MEM = ("channel-member-add", "channel-member-remove", "bot-create", "rollback:channel-member-add",
        "rollback:channel-member-remove")
_BOT = ("bot-create", "rollback:bot-create")
_ENA = ("bot-enable",)
# method, path (no query), allowed query keys, which actions (None — any, including the edge's own posts outside an action)
PATHS = [
    ("GET", r"/users/me", (), None), ("GET", rf"/users/{_I}", (), None), ("GET", r"/users/username/[a-z0-9._-]{1,64}", (), None),
    ("GET", r"/teams/name/[a-z0-9-]{1,64}", (), None), ("GET", rf"/teams/{_I}/channels/name/[a-z0-9_-]{{1,64}}", (), None),
    ("GET", rf"/channels/{_I}", (), None), ("GET", rf"/channels/{_I}/posts", ("page", "per_page"), None),
    ("GET", rf"/channels/{_I}/members", ("page", "per_page"), None), ("GET", rf"/channels/{_I}/members/{_I}", (), None),
    ("GET", rf"/posts/{_I}", (), None), ("GET", rf"/posts/{_I}/thread", (), None),
    ("POST", r"/posts", (), (None,)),
    ("POST", rf"/channels/{_I}/members", (), _MEM), ("DELETE", rf"/channels/{_I}/members/{_I}", (), _MEM),
    ("DELETE", rf"/posts/{_I}", ("permanent",), IRREVERSIBLE),
    ("GET", rf"/users/{_I}/teams/{_I}/channels/categories", (), None),
    ("POST", rf"/users/{_I}/teams/{_I}/channels/categories", (), _CAT),
    ("PUT", rf"/users/{_I}/teams/{_I}/channels/categories/{_I}", (), _CAT),
    ("DELETE", rf"/users/{_I}/teams/{_I}/channels/categories/{_I}", (), _CAT),
    ("POST", r"/bots", (), _BOT), ("GET", rf"/bots/{_I}", (), None), ("POST", rf"/bots/{_I}/assign/{_I}", (), _BOT),
    ("POST", rf"/bots/{_I}/disable", (), _BOT + ("rollback:bot-enable",)), ("POST", rf"/teams/{_I}/members", (), _BOT),
    ("GET", rf"/bots/{_I}", ("include_deleted",), _ENA + ("rollback:bot-enable",)),
    ("POST", rf"/bots/{_I}/enable", (), _ENA),
    ("GET", rf"/users/{_I}/tokens", ("page", "per_page"), _ENA), ("POST", r"/users/tokens/revoke", (), _ENA),
]


def path_ok(method: str, path: str, act: Optional[str]) -> bool:
    """Is the request in the list for the current action? ``permanent=true`` — only for deleting posts."""
    p, _, q = path.partition("?")
    qs = urllib.parse.parse_qs(q, keep_blank_values=True)
    for m, rx, keys, acts in PATHS:
        if m != method or not re.fullmatch(rx, p):
            continue
        if acts is not None and act not in acts:
            continue
        if any(k not in keys for k in qs):
            continue
        if "permanent" in qs and not (qs["permanent"] == ["true"] and act in IRREVERSIBLE):
            continue
        return True
    return False


class Admin:
    def __init__(self, edge):
        self.edge = edge
        e = edge.env
        self.env = e
        self.state = e.get("EDGE_ADMIN_STATE", "/edge-data/admin")
        self.action: Optional[str] = None

    # Mattermost through the list
    def api(self, method: str, path: str, body=None):
        if not path_ok(method, path, self.action):
            raise Refused(f"request {method} {path.split('?')[0]} is outside the list" +
                          (" (permanent=true — only for deleting posts)" if "permanent" in path else ""))
        return self.edge.api(method, path, body)

    def gone(self, method: str, path: str) -> bool:
        try:
            self.api(method, path)
            return False
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True
            raise

    # settings
    def secret(self) -> str:
        s = self.env.get("EDGE_ADMIN_SECRET", "")
        if not s:
            raise Refused("admin is not set up on the server: no EDGE_ADMIN_SECRET")
        # equal to a key that is also on the owners' machines — any session could sign admin buttons
        if any(s == self.env.get(k) for k in ("EDGE_SECRET", "EDGE_MASTER", "EDGE_SLASH_SECRET")):
            raise Refused("EDGE_ADMIN_SECRET equals another edge key — admin is off")
        return s

    def sig(self, ctx: dict) -> str:
        raw = json.dumps({k: v for k, v in ctx.items() if k != "sig"}, sort_keys=True, ensure_ascii=False)
        return hmac.new(self.secret().encode(), raw.encode(), hashlib.sha256).hexdigest()

    def confirmers(self) -> List[str]:
        return [x.strip() for x in self.env.get("EDGE_ADMIN_CONFIRMERS", "").split(",") if x.strip()]

    def log_channel(self) -> str:
        ch = self.env.get("EDGE_ADMIN_LOG_CHANNEL", "").strip()
        if not ID.fullmatch(ch):
            raise Refused("admin is not set up on the server: no EDGE_ADMIN_LOG_CHANNEL")
        return ch

    def origins(self) -> Dict[str, Tuple[str, ...]]:
        return parse_origins(self.env.get("EDGE_ADMIN_ORIGINS", ""), self.edge.owner_login)

    def journal(self, **rec) -> bool:
        rec = {"at": int(time.time() * 1000), **rec}
        try:
            os.makedirs(self.state, exist_ok=True)
            with open(os.path.join(self.state, "journal.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError as e:
            self.edge.log(f"admin: journal not written ({type(e).__name__})")
            return False

    def log_post(self, text: str) -> None:
        self.action = None
        try:
            self.api("POST", "/posts", {"channel_id": self.log_channel(), "message": text})
        except Exception as e:
            self.edge.log(f"admin: the log channel post failed ({type(e).__name__})")

    # reading helpers
    def _team(self, name):
        return self.api("GET", f"/teams/name/{name}")["id"]

    def _channel(self, tid, name):
        try:
            return self.api("GET", f"/teams/{tid}/channels/name/{name}")
        except urllib.error.HTTPError:
            raise Refused(f"no channel ~{name}")

    def _user(self, login):
        try:
            return self.api("GET", f"/users/username/{login}")
        except urllib.error.HTTPError:
            raise Refused(f"no user @{login}")

    def _uname(self, uid, cache):
        if uid not in cache:
            try:
                cache[uid] = self.api("GET", f"/users/{uid}")["username"]
            except Exception:
                cache[uid] = uid
        return cache[uid]

    def _members(self, cid):
        out, page = [], 0
        while page < 50:
            chunk = self.api("GET", f"/channels/{cid}/members?page={page}&per_page=200") or []
            if not chunk:
                break
            out += [m["user_id"] for m in chunk]
            page += 1
        return out

    def _channel_posts(self, cid):
        out, page = [], 0
        while page < 50:
            r = self.api("GET", f"/channels/{cid}/posts?page={page}&per_page=200") or {}
            order = r.get("order") or []
            if not order:
                break
            out += [r["posts"][i] for i in order if i in (r.get("posts") or {})]
            page += 1
        return out

    def _bot_tokens(self, uid):
        """Live tokens of a bot: disabling a bot does not revoke them — after enabling they would work again."""
        out, page = [], 0
        while page < 20:
            chunk = self.api("GET", f"/users/{uid}/tokens?page={page}&per_page=200") or []
            if not chunk:
                break
            out += [t["id"] for t in chunk if t.get("is_active", True)]
            page += 1
        return out

    @staticmethod
    def fp(action, target, resolved) -> str:
        raw = json.dumps({"action": action, "target": target, "resolved": resolved}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _cat(cats, name=None, cid=None):
        return next((x for x in cats.get("categories") or [] if (cid and x.get("id") == cid) or
                     (name and x.get("type") == "custom" and x.get("display_name") == name)), None)

    # the dry run
    def plan(self, action: str, t: dict) -> dict:
        """Read only → {lines, resolved, data}; Refused when it cannot be done."""
        self.action = action
        names: Dict[str, str] = {}
        me, log_ch = self.api("GET", "/users/me")["id"], self.log_channel()
        if action == "posts-delete":
            posts, chans = {}, {}
            for pid in t["post_ids"]:
                try:
                    p = self.api("GET", f"/posts/{pid}")
                except urllib.error.HTTPError:
                    raise Refused(f"no post {pid}")
                posts[pid] = p
                if not p.get("root_id"):          # a root takes its replies — they go into the copy and the count
                    th = self.api("GET", f"/posts/{pid}/thread") or {}
                    for rid, r in (th.get("posts") or {}).items():
                        posts.setdefault(rid, r)
            for p in posts.values():
                if p.get("channel_id") == log_ch or p.get("user_id") == me:
                    raise Refused(f"post {p['id']} is in the admin log or by the edge: the audit is not deleted")
                c = chans.get(p["channel_id"]) or self.api("GET", f"/channels/{p['channel_id']}")
                chans[c["id"]] = c
                if c.get("type") not in ("O", "P"):
                    raise Refused(f"post {p['id']} is in a DM or a group message: not allowed")
            per = Counter(chans[p["channel_id"]]["name"] for p in posts.values())
            authors = Counter(self._uname(p["user_id"], names) for p in posts.values())
            ids = sorted(posts)
            files = sum(len(p.get("file_ids") or []) for p in posts.values())
            lines = [f"Delete permanently {len(ids)} post(s) ({len(t['post_ids'])} in the request, the rest are their "
                     "thread replies):", *[f"- ~{n}: {k}" for n, k in sorted(per.items())],
                     "Authors: " + ", ".join(f"@{a} {k}" for a, k in authors.most_common(10)),
                     "First ids: " + ", ".join(ids[:10])]
            if files:
                lines.append(f"**Attachments: {files} — their files are deleted without a copy** (the copy keeps the "
                             "posts' text and props)")
            return {"lines": lines, "resolved": ids, "data": {"posts": [posts[i] for i in ids]}}
        if action == "system-messages-delete":
            tid = self._team(t["team"])
            c = self._channel(tid, t["channel"])
            if c.get("type") not in ("O", "P"):
                raise Refused("a DM or a group message: not allowed")
            if c["id"] == log_ch:
                raise Refused("the admin log channel — the audit is not deleted")
            sel = [p for p in self._channel_posts(c["id"]) if p.get("type") in t["types"]
                   and t["since"] <= p.get("create_at", 0) < t["until"]]
            by = Counter(p["type"] for p in sel)
            ids = sorted(p["id"] for p in sel)
            if not ids:
                raise Refused("no system messages in the period")
            lines = [f"Delete permanently system messages of ~{c['name']}: {len(ids)}",
                     *[f"- {k}: {n}" for k, n in sorted(by.items())]]
            return {"lines": lines, "resolved": ids, "data": {"posts": sorted(sel, key=lambda p: p["id"]), "channel": c["id"]}}
        if action == "sidebar-category":
            tid = self._team(t["team"])
            chans = [self._channel(tid, n) for n in t["channels"]]
            if any(c.get("type") not in ("O", "P") for c in chans):
                raise Refused("a DM or a group message: not allowed")
            people = [self._user(n)["id"] for n in t["users"]]
            for n in t["members_of"]:
                people += self._members(self._channel(tid, n)["id"])
            humans = []
            for uid in sorted(set(people)):
                u = self.api("GET", f"/users/{uid}")
                if not u.get("is_bot") and not u.get("delete_at"):
                    humans.append(u)
                    names[uid] = u["username"]
            if not humans:
                raise Refused("no people — nobody to arrange for")
            if len(humans) > MAX_PEOPLE:
                raise Refused(f"{len(humans)} people — more than {MAX_PEOPLE}")
            cids, per = sorted(c["id"] for c in chans), []
            for u in humans:
                cur = self._cat(self.api("GET", f"/users/{u['id']}/teams/{tid}/channels/categories") or {}, name=t["name"])
                missing = [i for i in cids if not cur or i not in (cur.get("channel_ids") or [])]
                if missing:
                    per.append([u["id"], "extend" if cur else "create", missing])
            if not per:
                raise Refused("everyone has it already — nothing to change")
            lines = [f"Category {code(t['name'], 22)}: channels " + ", ".join("~" + c["name"] for c in chans),
                     f"People: {len(per)} (create {sum(1 for p in per if p[1] == 'create')}, extend "
                     f"{sum(1 for p in per if p[1] == 'extend')}): "
                     + ", ".join("@" + names[p[0]] for p in per[:10]) + (" …" if len(per) > 10 else "")]
            return {"lines": lines, "resolved": per, "data": {"team": tid}}
        if action in ("channel-member-add", "channel-member-remove"):
            tid = self._team(t["team"])
            c = self._channel(tid, t["channel"])
            u = self._user(t["user"])
            if c.get("type") not in ("O", "P"):
                raise Refused("a DM or a group message: not allowed")
            if action == "channel-member-add" and c["type"] != "O":
                raise Refused(CLOSED_HINT)
            member = not self.gone("GET", f"/channels/{c['id']}/members/{u['id']}")
            if member != (action == "channel-member-remove"):
                # the members of a private channel are not told in a refusal (an oracle)
                raise Refused("the channel's members do not fit the action" if c["type"] == "P" else
                              f"@{u['username']} {'already in' if member else 'not in'} ~{c['name']}")
            kind = "public" if c["type"] == "O" else "private"
            line = (f"Add @{u['username']} to ~{c['name']} ({kind})" if action == "channel-member-add"
                    else f"Remove @{u['username']} from ~{c['name']} ({kind})")
            return {"lines": [line], "resolved": [c["id"], u["id"]], "data": {"channel": c["id"], "user": u["id"]}}
        if action == "bot-create":
            tid = self._team(t["team"])
            chans = [self._channel(tid, n) for n in t["channels"]]
            if any(c.get("type") != "O" for c in chans):
                raise Refused(CLOSED_HINT)
            if not self.gone("GET", f"/users/username/{t['username']}"):
                raise Refused(f"the name @{t['username']} is taken")
            o = self._user(t["owner"])
            if o.get("is_bot"):
                raise Refused(f"the owner @{t['owner']} is a bot: a bot's owner is a person")
            lines = [f"Bot @{t['username']} (name {code(t['display_name'], 64)}), owner @{o['username']}, team {t['team']}",
                     "Channels: " + (", ".join("~" + c["name"] for c in chans) or "—"),
                     f"No token is issued: the owner takes it with `/bot-token {t['username']}`."]
            return {"lines": lines, "resolved": [tid, o["id"], sorted(c["id"] for c in chans)],
                    "data": {"team": tid, "owner": o["id"], "channels": [c["id"] for c in chans]}}
        if action == "bot-enable":
            try:
                own = self.edge.edge_owner_id()
            except Exception:
                own = None
            if not own:
                raise Refused("admin is not set up on the server: no EDGE_OWNER — nobody to check 'own bot' against")
            u = self._user(t["username"])
            if not u.get("is_bot"):
                raise Refused(f"@{t['username']} is not a bot")
            b = self.api("GET", f"/bots/{u['id']}?include_deleted=true") or {}
            if b.get("owner_id") != own:
                raise Refused(f"the owner of @{t['username']} is not the edge owner: not enabling someone else's bot")
            if not b.get("delete_at"):
                raise Refused(f"the bot @{t['username']} is already enabled")
            toks = self._bot_tokens(u["id"])
            lines = [f"Enable the bot @{t['username']}, owner @{self._uname(own, names)}",
                     f"**Live tokens: {len(toks)} — revoked before enabling**, otherwise they would come back with the bot"
                     if toks else "No live tokens",
                     f"The owner takes a new token with `/bot-token {t['username']}`."]
            return {"lines": lines, "resolved": [u["id"], own, sorted(toks)], "data": {"bot": u["id"], "owner": own,
                                                                                       "tokens": sorted(toks)}}
        raise Refused(f"action {code(action, 40)} is not in the list")

    def rollback_plan(self, result: dict) -> dict:
        """A rollback of a result: only what the edge did (data in the state), 24 h, once. Deleting posts — irreversible."""
        action = result.get("action")
        if action in IRREVERSIBLE:
            raise Refused("irreversible: the copy is in the admin journal")
        if str(action).startswith("rollback:"):
            raise Refused("this is a rollback: there is no rollback of a rollback")
        if time.time() * 1000 - float(result.get("at") or 0) > ROLLBACK_TTL_S * 1000:
            raise Refused("a rollback — only within 24 h")
        bk = self.backup_read(result.get("backup"))
        undo = self.undo_read(result.get("backup"))
        if os.path.exists(os.path.join(self.state, "used", "rb-" + result["backup"])):
            raise Refused("already rolled back")
        self.action = "rollback:" + str(action)
        names: Dict[str, str] = {}
        if action == "sidebar-category":
            return {"lines": [f"Roll back the category {code(bk['request']['target']['name'], 22)}: remove the channels the "
                              f"edge added for {len(undo['cats'])} people (their own categories and channels stay): "
                              + ", ".join("@" + self._uname(c["uid"], names) for c in undo["cats"][:10])],
                    "resolved": undo["cats"], "data": {"team": bk["data"]["team"], "cats": undo["cats"]}, "of": action}
        if action in ("channel-member-add", "channel-member-remove"):
            d = bk["data"]
            c = self.api("GET", f"/channels/{d['channel']}")
            if action == "channel-member-remove" and c.get("type") != "O":
                raise Refused(CLOSED_HINT)
            who = self._uname(d["user"], names)
            line = (f"Remove @{who} from ~{c['name']}" if action == "channel-member-add"
                    else f"Put @{who} back into ~{c['name']} (public)")
            return {"lines": [f"Rollback: {line}"], "resolved": [d["channel"], d["user"]], "data": d, "of": action}
        if action in ("bot-create", "bot-enable"):
            bid = undo.get("bot")
            if not bid or not ID.fullmatch(bid):
                raise Refused("the rollback data has no bot id")
            return {"lines": [f"Rollback: disable the bot @{self._uname(bid, names)}"], "resolved": [bid],
                    "data": {"bot": bid}, "of": action}
        raise Refused(f"no rollback for {action}")

    # backups and undo data
    @staticmethod
    def _state_name(name):
        if not isinstance(name, str) or not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}-[a-z:-]{3,40}\.json", name):
            raise Refused("no copy to roll back from")
        return name

    def backup_write(self, action, req, plan) -> str:
        d = os.path.join(self.state, "backup")
        os.makedirs(d, exist_ok=True)
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}-{action.replace(':', '-')}.json"
        with open(os.path.join(d, name), "x", encoding="utf-8") as f:
            json.dump({"action": action, "request": req, "resolved": plan["resolved"], "data": plan["data"]}, f,
                      ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        return name

    def backup_read(self, name):
        with open(os.path.join(self.state, "backup", self._state_name(name)), encoding="utf-8") as f:
            return json.load(f)

    def undo_write(self, name, undo):
        d = os.path.join(self.state, "undo")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            json.dump(undo, f, ensure_ascii=False)

    def undo_read(self, name):
        try:
            with open(os.path.join(self.state, "undo", self._state_name(name)), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    # execution
    def execute(self, action, plan, undo):
        """→ (done, total). ``undo`` fills as it goes: after a failure midway the rollback knows what was done."""
        self.action = action
        d, done = plan["data"], 0
        if action in IRREVERSIBLE:
            for p in d["posts"]:
                try:
                    self.api("DELETE", f"/posts/{p['id']}?permanent=true")
                except urllib.error.HTTPError as e:
                    if e.code != 404:
                        self.edge.log(f"admin: DELETE {p['id']}: {e.code}")
                if self.gone("GET", f"/posts/{p['id']}"):
                    done += 1
            return done, len(d["posts"])
        if action == "sidebar-category":
            tid = d["team"]
            undo.setdefault("cats", [])
            for uid, kind, missing in plan["resolved"]:
                base = f"/users/{uid}/teams/{tid}/channels/categories"
                cur = self._cat(self.api("GET", base) or {}, name=plan["name"])
                if cur:
                    cur = dict(cur)
                    have = list(cur.get("channel_ids") or [])
                    added = [i for i in missing if i not in have]
                    cur["channel_ids"] = have + added
                    self.api("PUT", f"{base}/{cur['id']}", cur)
                    undo["cats"].append({"uid": uid, "cat": cur["id"], "created": False, "added": added})
                else:
                    c = self.api("POST", base, {"user_id": uid, "team_id": tid, "display_name": plan["name"],
                                                "type": "custom", "channel_ids": missing})
                    undo["cats"].append({"uid": uid, "cat": c["id"], "created": True, "added": missing})
                cur = self._cat(self.api("GET", base) or {}, name=plan["name"])
                if cur and all(i in (cur.get("channel_ids") or []) for i in missing):
                    done += 1
            return done, len(plan["resolved"])
        if action == "channel-member-add":
            self.api("POST", f"/channels/{d['channel']}/members", {"user_id": d["user"]})
            return (0 if self.gone("GET", f"/channels/{d['channel']}/members/{d['user']}") else 1), 1
        if action == "channel-member-remove":
            self.api("DELETE", f"/channels/{d['channel']}/members/{d['user']}")
            return (1 if self.gone("GET", f"/channels/{d['channel']}/members/{d['user']}") else 0), 1
        if action == "bot-create":
            t = plan["target"]
            b = self.api("POST", "/bots", {"username": t["username"], "display_name": t["display_name"],
                                           "description": t["description"]})
            undo["bot"] = bid = b["user_id"]
            self.api("POST", f"/bots/{bid}/assign/{d['owner']}")
            self.api("POST", f"/teams/{d['team']}/members", {"team_id": d["team"], "user_id": bid})
            for c in d["channels"]:
                self.api("POST", f"/channels/{c}/members", {"user_id": bid})
            return (1 if (self.api("GET", f"/bots/{bid}") or {}).get("owner_id") == d["owner"] else 0), 1
        if action == "bot-enable":
            for tid in d["tokens"]:       # before enabling: no window where an old token works
                self.api("POST", "/users/tokens/revoke", {"token_id": tid})
            if self._bot_tokens(d["bot"]):
                raise Refused("old tokens were not revoked — the bot is not enabled")
            self.api("POST", f"/bots/{d['bot']}/enable")
            undo["bot"] = d["bot"]
            b = self.api("GET", f"/bots/{d['bot']}?include_deleted=true") or {}
            return (1 if not b.get("delete_at") and b.get("owner_id") == d["owner"] else 0), 1
        raise Refused(f"action {action} is not in the list")

    def execute_rollback(self, plan):
        of, d = plan["of"], plan["data"]
        self.action = "rollback:" + of
        if of == "sidebar-category":
            tid, done = d["team"], 0
            for c in d["cats"]:
                base = f"/users/{c['uid']}/teams/{tid}/channels/categories"
                cur = self._cat(self.api("GET", base) or {}, cid=c["cat"])
                if cur:
                    rest = [i for i in cur.get("channel_ids") or [] if i not in c["added"]]
                    if c["created"] and not rest:
                        self.api("DELETE", f"{base}/{c['cat']}")
                    else:
                        self.api("PUT", f"{base}/{c['cat']}", dict(cur, channel_ids=rest))
                cur = self._cat(self.api("GET", base) or {}, cid=c["cat"])
                if not cur or not any(i in (cur.get("channel_ids") or []) for i in c["added"]):
                    done += 1
            return done, len(d["cats"])
        if of == "channel-member-add":
            self.api("DELETE", f"/channels/{d['channel']}/members/{d['user']}")
            return (1 if self.gone("GET", f"/channels/{d['channel']}/members/{d['user']}") else 0), 1
        if of == "channel-member-remove":
            self.api("POST", f"/channels/{d['channel']}/members", {"user_id": d["user"]})
            return (0 if self.gone("GET", f"/channels/{d['channel']}/members/{d['user']}") else 1), 1
        if of in ("bot-create", "bot-enable"):
            self.api("POST", f"/bots/{d['bot']}/disable")
            return 1, 1
        raise Refused(f"no rollback for {of}")

    # the post, the command, the press
    def post_text(self, action, plan, fp, req, caller, rollback) -> str:
        first, *rest = plan["lines"]
        if action in IRREVERSIBLE:
            tail = "**irreversible**: what is deleted does not come back; the only trace is the copy in the admin journal"
        elif rollback:
            tail = "this is a rollback: there is no rollback of it"
        else:
            tail = f"rollback — `/{command_name()} rollback <id of this post>`, 24 h"
        who = ", ".join("@" + self._uname(i, {}) for i in self.confirmers()) or "nobody (EDGE_ADMIN_CONFIRMERS is empty)"
        return "\n".join([f"🛠 **admin · {action}:** {first}", *rest, tail, f"Fingerprint: `{fp[:16]}`",
                          f"Asks: {code(req.get('origin'), 40)} · bot @{caller} · why: {code(req.get('why'))}",
                          f"Can press: {who}; 15 minutes."])

    def buttons(self, ctx: dict) -> list:
        url = self.edge.action_url[:-len("/action")] + BUTTON_PATH
        acts = []
        for aid, name, ans in (("adminyes", "✅ do it", "yes"), ("adminno", "❌ no", "no")):
            c = dict(ctx, answer=ans)
            c["sig"] = self.sig(c)
            acts.append({"id": aid, "name": name, "integration": {"url": url, "context": c}})
        return [{"actions": acts}]

    def caller(self, uid: str) -> dict:
        want = self.env.get("EDGE_ADMIN_REQUESTER", "").strip()
        if not want:
            raise Refused("admin is not set up on the server: no EDGE_ADMIN_REQUESTER")
        self.action = None
        caller = self.api("GET", f"/users/{uid}") if ID.fullmatch(uid or "") else {}
        if uid != want or not caller.get("is_bot"):
            raise Refused(f"/{command_name()} is called only by the admin role's bot")
        return caller

    def command(self, form: Dict[str, str], args: List[str]) -> str:
        uid = form.get("user_id", "")
        want = self.env.get("EDGE_ADMIN_CMD_TOKEN", "")
        if not want or not hmac.compare_digest(str(form.get("token", "")).encode(), want.encode()):
            self.journal(action=None, result="refused", why="command token", requester=uid)
            return "admin: the command token does not match — refused."
        if not args or args[0] == "help":
            return HELP()
        try:
            self.secret()
            log_ch = self.log_channel()
            caller = self.caller(uid)
            rollback = args[0] == "rollback"
            pid = args[1] if rollback and len(args) > 1 else args[0]
            if not ID.fullmatch(pid):
                raise Refused("a post id is needed")
            post = self.api("GET", f"/posts/{pid}")
            if rollback:
                me = self.api("GET", "/users/me")["id"]
                res = (post.get("props") or {}).get(RESULT)
                if post.get("user_id") != me or post.get("channel_id") != log_ch or not isinstance(res, dict):
                    raise Refused("this is not an admin result post")
                if res.get("result") not in ("done", "partial", "error"):
                    raise Refused("nothing to roll back: the action was not executed")
                plan = self.rollback_plan(res)
                action, req, target = "rollback:" + res["action"], res.get("request") or {}, {"result_post": pid}
            else:
                if post.get("user_id") != uid:
                    raise Refused("the request is posted by the same bot that calls the command")
                if post.get("channel_id") != log_ch:
                    raise Refused("a request — only in the admin log channel")
                req = (post.get("props") or {}).get(REQ)
                action, target = validate(req, self.origins())
                plan = self.plan(action, target)
            fp = self.fp(action, target, plan["resolved"])
            ctx = {"kind": "admin", "post": pid, "rb": rollback, "fp": fp, "at": int(time.time()),
                   "nonce": secrets.token_hex(8)}
            msg = self.post_text(action, plan, fp, req, caller.get("username"), rollback)
            self.action = None
            p = self.api("POST", "/posts", {"channel_id": log_ch, "root_id": post.get("root_id") or post["id"],
                                            "message": msg, "props": {"attachments": self.buttons(ctx),
                                                                      CONFIRM: {"post": pid, "action": action, "fp": fp,
                                                                                "nonce": ctx["nonce"]}}})
            self.journal(action=action, target=pid, requester=caller.get("username"), origin=req.get("origin"), dry=True,
                         fingerprint=fp, result="asked", confirm_post=p.get("id"))
            return f"admin: dry run ready, confirmation post {p.get('id')} waits for a press."
        except Refused as e:
            self.journal(action=(args[0] if args else None), requester=uid, result="refused", why=str(e))
            return f"admin: refused — {e}"

    def own_confirm(self, body, ctx) -> dict:
        """A press counts only on the edge's confirmation post whose fingerprint and nonce equal the button's."""
        pid = body.get("post_id") or ""
        if not ID.fullmatch(pid):
            raise Refused("the button is not on a confirmation post")
        self.action = None
        own = self.api("GET", f"/posts/{pid}")
        conf = (own.get("props") or {}).get(CONFIRM) or {}
        if (own.get("user_id") != self.api("GET", "/users/me")["id"] or own.get("channel_id") != self.log_channel()
                or conf.get("post") != ctx.get("post") or not hmac.compare_digest(str(conf.get("fp")), str(ctx.get("fp")))
                or not hmac.compare_digest(str(conf.get("nonce")), str(ctx.get("nonce")))):
            raise Refused("the button is not on its own confirmation post")
        return own

    def press(self, body: dict) -> dict:
        ctx = body.get("context") or {}
        try:
            if ctx.get("kind") != "admin" or not hmac.compare_digest(str(ctx.get("sig") or "").encode(),
                                                                     self.sig(ctx).encode()):
                raise Refused("the button signature does not match")
            user = body.get("user_id", "")
            if user not in self.confirmers():
                raise Refused("only a confirmer presses this — the press does not count")
            if time.time() - float(ctx.get("at") or 0) > TTL_S:
                raise Refused("15 minutes passed — ask again")
            if not re.fullmatch(r"[0-9a-f]{16}", str(ctx.get("nonce"))):
                raise Refused("the button signature does not match")
            own = self.own_confirm(body, ctx)
            os.makedirs(os.path.join(self.state, "used"), exist_ok=True)
            try:
                os.close(os.open(os.path.join(self.state, "used", ctx["nonce"]), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            except FileExistsError:
                raise Refused("already pressed")
            by = self._uname(user, {})
            if ctx.get("answer") != "yes":
                self.journal(action=None, target=ctx.get("post"), confirmed_by=by, fingerprint=ctx.get("fp"),
                             result="declined")
                return {"update": {"message": (own.get("message") or "") + f"\n\n❌ declined by @{by}",
                                   "props": {"attachments": []}}}
            post = self.api("GET", f"/posts/{ctx['post']}")
            if ctx.get("rb"):
                res = (post.get("props") or {}).get(RESULT) or {}
                plan = self.rollback_plan(res)
                action, req, target = "rollback:" + str(res.get("action")), res.get("request") or {}, {"result_post": ctx["post"]}
            else:
                req = (post.get("props") or {}).get(REQ)
                action, target = validate(req, self.origins())
                plan = self.plan(action, target)
            fp = self.fp(action, target, plan["resolved"])
            if not hmac.compare_digest(fp, str(ctx.get("fp"))):
                raise Refused("the fingerprint does not match — the targets changed, none is touched")
            if action == "bot-enable" and plan["data"]["owner"] != user:
                raise Refused("only its owner enables their own bot")
            try:
                bk = self.backup_write(action, req, plan)
            except OSError as e:
                raise Refused(f"the copy was not written ({type(e).__name__}) — nothing done")
            if not self.journal(action=action, target=ctx.get("post"), origin=req.get("origin"), fingerprint=fp,
                                confirmed_by=by, backup=bk, result="started"):
                raise Refused("the journal was not written — nothing done")
            if ctx.get("rb"):             # "rolled back" — after the journal: without the journal it does not burn
                try:
                    os.close(os.open(os.path.join(self.state, "used", "rb-" + res["backup"]),
                                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                except FileExistsError:
                    raise Refused("already rolled back")
            undo, done, total, err = {}, 0, 0, None
            try:
                if ctx.get("rb"):
                    done, total = self.execute_rollback(plan)
                else:
                    plan["target"], plan["name"] = target, target.get("name")
                    done, total = self.execute(action, plan, undo)
            except Exception as e:        # a failure midway — the result, the journal and the rollback data still
                err = f"{type(e).__name__}: {e}"[:200]
                self.edge.log(f"admin: {action}: failure midway: {err}")
            finally:
                if undo:
                    try:
                        self.undo_write(bk, undo)
                    except OSError as e:
                        self.edge.log(f"admin: rollback data not written ({type(e).__name__})")
            result = "error" if err else ("done" if done == total else "partial")
            line = (f"❌ failure midway ({err}) — see the copy" if err else
                    f"✅ done {done} of {total}" if result == "done" else f"⚠️ partial {done} of {total}")
            msg = (own.get("message") or "") + f"\n\n{line} · pressed by @{by} · copy {bk}"
            props = {"attachments": [], CONFIRM: (own.get("props") or {}).get(CONFIRM),
                     RESULT: {"action": action, "request": req, "result": result, "done": done, "total": total,
                              "backup": bk, "fp": fp, "at": int(time.time() * 1000),
                              "rollback_of": ctx["post"] if ctx.get("rb") else None}}
            self.journal(action=action, target=ctx.get("post"), origin=req.get("origin"), dry=False, fingerprint=fp,
                         confirmed_by=by, backup=bk, result=result, done=done, total=total, error=err,
                         rollback_of=ctx["post"] if ctx.get("rb") else None)
            self.log_post(f"admin: {action} — {line}, asked by {code(req.get('origin'), 40)}, pressed by @{by}, "
                          f"fingerprint `{fp[:16]}`")
            return {"update": {"message": msg, "props": props}}
        except Refused as e:
            self.journal(action=None, target=ctx.get("post"), requester=body.get("user_id"), result="refused", why=str(e))
            return {"ephemeral_text": f"admin: refused — {e}"}


# where the confirmation buttons call the edge; a server moving from an earlier version keeps its old path
# (``EDGE_ADMIN_BUTTON_PATH``), so confirmation posts made before the swap still accept presses
BUTTON_PATH = os.environ.get("EDGE_ADMIN_BUTTON_PATH", "").strip() or "/admin-press"
if not re.fullmatch(r"/[a-z][a-z0-9-]{0,31}", BUTTON_PATH):
    raise SystemExit(f"aimost admin: EDGE_ADMIN_BUTTON_PATH={BUTTON_PATH!r} — expected /[a-z][a-z0-9-]{{0,31}}")


def command_name() -> str:
    return (os.environ.get("AIMOST_ADMIN_COMMAND", "").strip() or "admin")


def HELP() -> str:
    n = command_name()
    return (f"`/{n} <request post id>` — a dry run and a post with buttons for the confirmer · "
            f"`/{n} rollback <result post id>` — a rollback. The request is made by `aimost admin request`.")


_ADMINS: Dict[int, Admin] = {}


def admin(edge) -> Admin:
    a = _ADMINS.get(id(edge))
    if a is None:
        a = _ADMINS[id(edge)] = Admin(edge)
    return a


def handler(call) -> str:
    """The slash command (an edge command): the core already resolved the caller; the executor checks the rest."""
    return admin(call.edge).command(call.form, call.args)


def route(edge, body: dict):
    """The buttons of the confirmation posts (``POST <edge>/admin-press``)."""
    try:
        return 200, admin(edge).press(body)
    except Exception as e:
        edge.log(f"admin: press: {type(e).__name__}")
        return 200, {"ephemeral_text": "admin: did not work — see the edge log; to retry, make a new request."}
