"""The loop arbiter (delivery spec, "Agent loop guard"): bots of different people answering each other forever.

The bridge on each owner's machine sees only its own bots, so a loop between bots of two people is caught by the edge,
which sees every post. Every ``EDGE_LOOP_EVERY_S`` (20) the edge reads new posts of the channels with bots (not DMs or group
messages, not ``EDGE_LOOP_FREE_CHANNELS``) and counts, per thread, the chain of bot → bot addressing without a person:

* a bot post addresses bot B by ``@B``; when both bots have the same human owner (``owner_id``), also when B started the
  thread, B owns the channel (``EDGE_CHANNEL_OWNERS=<channel>:<bot login>``), or B wrote in the thread after being
  mentioned there by the author earlier;
* a link is a post of B after another bot's post addressed to B; the chain goes on when its last link addresses the author
  of the next one. A person's post, or a bot post a person touched (an answer button, a confirmation, a reaction), breaks it.

More than ``EDGE_LOOP_LIMIT`` (6) links, at least two authors — a signed mark ``<prefix>_loop_pause`` on the thread root
(no post in the thread). Bridges do not wake roles by bot posts of a paused thread; the mark's owners see it as a line in
"Waiting for you" with "continue". It is lifted by a person's post in the thread or by "continue" pressed by one of the
mark's owners. Active pauses live in the edge bot's preferences (``<prefix>_loop_pause/<root>``), lifted-by-button ones —
``<prefix>_loop_cleared/<root>`` (the chain counts again from that moment); a mark that is not among the active pauses is
removed. The mark is renewed every ``EDGE_LOOP_RENEW_S`` (6 h); a bridge ignores one not renewed for ``EDGE_LOOP_MAX_S`` (24 h).

Signatures: HMAC(key, canonical JSON of the mark without ``sigs`` | root id) with each live owner key (``kid``) and with
the shared ``EDGE_SECRET`` (as ``*``) — no new secret; a bridge checks the one of its own key. The text of posts is
neither stored nor logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from typing import Dict, List, Optional

from .names import P

MENTION_RE = re.compile(r"(?<![\w.])@([a-z0-9][a-z0-9._-]*[a-z0-9])")
ID_RE = re.compile(r"[a-z0-9]{26}")
LIST_S = 600
KEEP_S = 7 * 86400
MARK = P("loop_pause")
PAUSE_CAT, CLEARED_CAT = P("loop_pause"), P("loop_cleared")


def canon(mark: dict, root: str) -> bytes:
    body = json.dumps({k: v for k, v in mark.items() if k != "sigs"}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return f"{body}|{root}".encode()


def _hm(key: str, msg: bytes) -> str:
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()


# ── the bridge side ──────────────────────────────────────────────────────────────────────────────────────────────────
def max_s() -> float:
    return float(os.environ.get("EDGE_LOOP_MAX_S", str(86400)))


def valid_mark(conn: dict, root: Optional[dict], now_ms: Optional[float] = None) -> Optional[dict]:
    """The pause mark on a thread root if signed with this connection's button key (``EDGE_KEY_ID`` → ``sigs[kid]``,
    else the shared ``sigs["*"]``) and renewed within ``EDGE_LOOP_MAX_S``; else None."""
    m = ((root or {}).get("props") or {}).get(MARK)
    sec, kid = conn.get("EDGE_SECRET", ""), conn.get("EDGE_KEY_ID") or "*"
    if not isinstance(m, dict) or not sec or not (root or {}).get("id") or not isinstance(m.get("sigs"), dict):
        return None
    if not hmac.compare_digest(str(m["sigs"].get(kid) or "").encode(), _hm(sec, canon(m, root["id"])).encode()):
        return None
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    if not isinstance(m.get("renewed"), (int, float)) or isinstance(m.get("renewed"), bool) \
            or now_ms - m["renewed"] > max_s() * 1000:
        return None
    return m


# ── the edge side ────────────────────────────────────────────────────────────────────────────────────────────────────
class Arbiter:
    def __init__(self, edge):
        e = edge.env
        self.edge = edge
        self.limit = int(e.get("EDGE_LOOP_LIMIT", "6"))
        self.every = float(e.get("EDGE_LOOP_EVERY_S", "20"))
        self.renew = float(e.get("EDGE_LOOP_RENEW_S", str(6 * 3600)))
        self.tail = int(e.get("EDGE_LOOP_TAIL", "200"))   # a long thread: its root and its tail only
        self.free = {x.strip() for x in e.get("EDGE_LOOP_FREE_CHANNELS", "").split(",") if x.strip()}
        self.chan_owners = dict(x.split(":", 1) for x in e.get("EDGE_CHANNEL_OWNERS", "").replace(" ", "").split(",")
                                if ":" in x)
        self.since: Dict[str, int] = {}
        self._bots, self._bots_at, self._chans, self._chans_at = None, 0.0, None, 0.0
        self._noowner = set()

    # preferences of the edge bot
    def _prefs(self, uid, cat):
        from .edgecmds import prefs_get
        return prefs_get(self.edge, uid, cat)

    def _put(self, uid, cat, name, value):
        from .edgecmds import pref_put
        pref_put(self.edge, uid, cat, name, value)

    def _del(self, uid, cat, names):
        from .edgecmds import pref_del
        pref_del(self.edge, uid, cat, names)

    def api(self, method, path, body=None):
        return self.edge.api(method, path, body)

    def sign(self, mark: dict, root: str) -> Dict[str, str]:
        msg, out = canon(mark, root), {}
        if self.edge.secret:
            out["*"] = _hm(self.edge.secret, msg)
        d = self.edge.keys() if self.edge.master else None
        for kid, e in (d or {}).items():
            if isinstance(e, dict) and not e.get("revoked") and e.get("owner_id"):
                out[kid] = _hm(self.edge.key_for(kid, e.get("gen", 1)), msg)
        return out

    def bots(self) -> Dict[str, dict]:
        """user_id → {name, owner}; owner — the human owner's id, None when the bot owns itself or a plugin does."""
        if self._bots is not None and time.time() - self._bots_at < LIST_S:
            return self._bots
        raw, page = [], 0
        while True:
            chunk = self.api("GET", f"/bots?page={page}&per_page=200&include_deleted=false") or []
            raw += chunk
            if len(chunk) < 200:
                break
            page += 1
        ids = {b["user_id"] for b in raw}
        out = {}
        for b in raw:
            o = b.get("owner_id") or ""
            human = bool(ID_RE.fullmatch(o)) and o != b["user_id"] and o not in ids
            out[b["user_id"]] = {"name": b.get("username") or "", "owner": o if human else None}
            if not human and b["user_id"] not in self._noowner:
                self._noowner.add(b["user_id"])
                self.edge.log(f"loop: bot @{b.get('username') or b['user_id']} has no human owner")
        self._bots, self._bots_at = out, time.time()
        return out

    def channels(self, bots) -> Dict[str, str]:
        if self._chans is not None and time.time() - self._chans_at < LIST_S:
            return self._chans
        out = {}
        for bid in bots:
            try:
                teams = self.api("GET", f"/users/{bid}/teams") or []
            except Exception as e:
                self.edge.log(f"loop: teams of {bid}: {type(e).__name__}")
                continue
            for t in teams:
                try:
                    chans = self.api("GET", f"/users/{bid}/teams/{t['id']}/channels") or []
                except Exception as e:
                    self.edge.log(f"loop: channels of {bid}: {type(e).__name__}")
                    continue
                for c in chans:          # DMs and group messages are dropped before reading posts
                    if c.get("type") in ("O", "P") and c.get("name") not in self.free:
                        out[c["id"]] = c.get("name") or ""
        self._chans, self._chans_at = out, time.time()
        return out

    @staticmethod
    def touched(x: dict, bots) -> bool:
        """A person took part in a bot's post: an answer button, a confirmation, a reaction."""
        pr = x.get("props") or {}
        if any(isinstance(a, dict) and a.get("user_id") and a["user_id"] not in bots
               for a in (pr.get(P("answers")) or {}).values()):
            return True
        c = pr.get(P("confirm")) or {}
        if isinstance(c, dict) and c.get("user_id") and c["user_id"] not in bots:
            return True
        return any(r.get("user_id") not in bots for r in (x.get("metadata") or {}).get("reactions") or [])

    def chain(self, posts: List[dict], bots, chan_owner: Optional[str], after: int = 0) -> List[dict]:
        """The bot → bot chain at the end of a thread (posts by create_at, the root first) → the link posts."""
        if len(posts) > self.tail + 1:
            posts = posts[:1] + posts[-self.tail:]
        by_name = {v["name"]: b for b, v in bots.items()}
        root = posts[0] if posts else {}

        def same(a, b):
            return bool(bots[a]["owner"]) and bots[a]["owner"] == bots[b]["owner"]
        wrote, said = {}, {}
        special = {x for x in (root.get("user_id"), by_name.get(chan_owner or "")) if x in bots}
        chain, addr = [], {}
        for i, p in enumerate(posts):
            a = p.get("user_id")
            if p.get("type"):
                continue
            named = {by_name[n] for n in set(MENTION_RE.findall(p.get("message") or "")) if n in by_name}
            if a not in bots or (p.get("create_at", 0) <= after and i) or self.touched(p, bots):
                chain = []
            else:
                prev = wrote.get(a, -1)
                if any(a in addr.get(j, ()) for j in range(prev + 1, i)):
                    if chain and a in addr.get(chain[-1], ()):
                        chain.append(i)
                    else:
                        chain = [i]
                near = (special | set(wrote)) - named - {a}
                addr[i] = (named - {a}) | {b for b in near if same(a, b) and (b in special or b in said.get(a, set()))}
            if a in bots:
                wrote[a] = i
                said.setdefault(a, set()).update(named)
        return [posts[i] for i in chain]

    def patch_root(self, root_id: str, mark: Optional[dict]) -> None:
        post = self.api("GET", f"/posts/{root_id}") or {}
        props = dict(post.get("props") or {})
        if mark is None:
            if MARK not in props:
                return
            props.pop(MARK)
        else:
            props[MARK] = mark
        self.api("PUT", f"/posts/{root_id}/patch", {"props": props})

    def clear_one(self, me: str, root_id: str, why: str) -> None:
        self.patch_root(root_id, None)
        self._del(me, PAUSE_CAT, [root_id])
        self.edge.log(f"loop: thread {root_id} — pause lifted ({why})")

    def thread(self, me, root_id, chan_name, bots, active, cleared, now) -> None:
        th = self.api("GET", f"/posts/{root_id}/thread") or {}
        posts = sorted((th.get("posts") or {}).values(), key=lambda x: (x.get("id") != root_id, x.get("create_at", 0)))
        if not posts or posts[0].get("id") != root_id:
            return
        root = posts[0]
        mark = (root.get("props") or {}).get(MARK)
        if mark:
            if not isinstance(mark, dict) or root_id not in active or active[root_id].get("id") != mark.get("id"):
                self.patch_root(root_id, None)
                self.edge.log(f"loop: thread {root_id} — a mark the edge did not set, removed")
                return
            if any(not p.get("type") and p.get("user_id") not in bots and p.get("create_at", 0) > mark.get("at", 0)
                   for p in posts[1:]):
                return self.clear_one(me, root_id, "a person's post")
            if now * 1000 - mark.get("renewed", 0) >= self.renew * 1000:
                m = dict(mark, renewed=int(now * 1000))
                m.pop("sigs", None)
                m["sigs"] = self.sign(m, root_id)
                self.patch_root(root_id, m)
                self.edge.log(f"loop: thread {root_id} — pause renewed")
            return
        if root_id in active:            # the mark was removed past the edge — the record no longer holds
            self._del(me, PAUSE_CAT, [root_id])
        chain = self.chain(posts, bots, self.chan_owners.get(chan_name), cleared.get(root_id, {}).get("at", 0))
        authors = sorted({bots[p["user_id"]]["name"] for p in chain})
        if len(chain) <= self.limit or len(authors) < 2:
            return
        owners = set()
        for p in chain:
            o = bots[p["user_id"]]["owner"]
            if o:
                try:
                    owners.add(self.api("GET", f"/users/{o}")["username"])
                except Exception:
                    pass
        at = int(now * 1000)
        m = {"id": secrets.token_hex(8), "at": at, "renewed": at, "authors": authors, "owners": sorted(owners),
             "links": len(chain), "last": chain[-1]["id"]}
        m["sigs"] = self.sign(m, root_id)
        self.patch_root(root_id, m)
        self._put(me, PAUSE_CAT, root_id, {"id": m["id"], "at": at})
        self.edge.log(f"loop: thread {root_id} — paused: {' ↔ '.join(authors)}, {len(chain)} links, "
                      f"owners {','.join(sorted(owners)) or '—'}")

    def pass_(self, now: Optional[float] = None) -> None:
        if not self.edge.secret and not self.edge.master:
            self.edge.log("loop: neither EDGE_SECRET nor EDGE_MASTER — nothing to sign a mark with, the arbiter is off")
            return
        now = now or time.time()
        me = self.api("GET", "/users/me")["id"]
        bots = self.bots()
        chans = self.channels(bots)
        active, cleared = self._prefs(me, PAUSE_CAT), self._prefs(me, CLEARED_CAT)
        old = [k for k, v in cleared.items() if now * 1000 - (v or {}).get("at", 0) > KEEP_S * 1000]
        if old:
            self._del(me, CLEARED_CAT, old)
        roots: Dict[str, str] = {}
        for cid, cname in chans.items():
            since = self.since.get(cid) or int((now - max(self.every * 3, 60)) * 1000)
            try:
                r = self.api("GET", f"/channels/{cid}/posts?since={since}") or {}
            except Exception as e:
                self.edge.log(f"loop: posts of {cid}: {type(e).__name__}")
                continue
            top = since
            for p in (r.get("posts") or {}).values():
                top = max(top, p.get("update_at", 0), p.get("create_at", 0))
                rid = p.get("root_id") or (p["id"] if MARK in (p.get("props") or {}) or p["id"] in active else "")
                if rid:
                    roots[rid] = cname
            self.since[cid] = top
        for rid in active:               # renewal and lifting — in threads without new posts too
            roots.setdefault(rid, "")
        for rid, cname in roots.items():
            try:
                self.thread(me, rid, cname, bots, active, cleared, now)
            except Exception as e:
                self.edge.log(f"loop: thread {rid}: {type(e).__name__}")

    def clear(self) -> None:
        me = self.api("GET", "/users/me")["id"]
        for rid in self._prefs(me, PAUSE_CAT):
            try:
                self.clear_one(me, rid, "loop-clear")
            except Exception as e:
                self.edge.log(f"loop: loop-clear {rid}: {type(e).__name__}")

    def cont(self, root_id: str, uid: str, kid: Optional[str] = None) -> Optional[str]:
        """"continue" in the pin: the button context is not trusted — the mark is read from the root and must be an active
        one; the presser is a person among its owners; a button with an owner key — that key's owner among them too.
        → None (lifted) or the refusal."""
        if not ID_RE.fullmatch(root_id or ""):
            return "not that thread"
        me = self.api("GET", "/users/me")["id"]
        mark = ((self.api("GET", f"/posts/{root_id}") or {}).get("props") or {}).get(MARK)
        act = self._prefs(me, PAUSE_CAT).get(root_id) or {}
        if not isinstance(mark, dict) or not mark.get("id") or act.get("id") != mark["id"]:
            return "this thread has no pause"
        owners = set(mark.get("owners") or [])
        u = self.api("GET", f"/users/{uid}") or {}
        if u.get("is_bot") or u.get("username") not in owners:
            return "only an owner of this loop's bots lifts the pause"
        if kid:
            e = (self.edge.keys() or {}).get(str(kid)) or {}
            try:
                ko = (self.api("GET", f"/users/{e.get('owner_id')}") or {}).get("username") if e.get("owner_id") else None
            except Exception:
                ko = None
            if ko not in owners:
                return "the button is not of an owner of this loop's bots"
        self.patch_root(root_id, None)
        self._del(me, PAUSE_CAT, [root_id])
        self._put(me, CLEARED_CAT, root_id, {"at": int(time.time() * 1000)})
        self.edge.log(f"loop: thread {root_id} — pause lifted (continue: {u.get('username')})")
        return None

    def loop(self) -> None:
        while True:
            time.sleep(self.every)
            try:
                self.pass_()
            except Exception as e:
                self.edge.log(f"loop: pass: {type(e).__name__}")


_ARB: Dict[int, Arbiter] = {}


def arbiter(edge) -> Arbiter:
    a = _ARB.get(id(edge))
    if a is None:
        a = _ARB[id(edge)] = Arbiter(edge)
    return a


def start(edge) -> None:
    if edge.env.get("EDGE_LOOP", "on") == "off":
        edge.log("loop: EDGE_LOOP=off — the loop arbiter is not started")
        return
    threading.Thread(target=arbiter(edge).loop, name="loop-arbiter", daemon=True).start()
