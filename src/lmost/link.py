"""The bridge: chat posts → session inboxes (delivery spec), receipts, answers, command posts, heartbeat (status spec).

An agent role is a chat participant through its bot: people mention it, call it into threads, write to it directly; it
works in a session on the owner's machine. The bridge talks to every chat server over outbound connections only: it
reads the channels of each role bot (``posts?since=`` returns new and edited posts), and a websocket per bot only wakes
the pass (``ws.Watcher``); without a live websocket the connection is polled every tick.

**Who gets a post** — the routes of the connection (``<config>/connections/<name>.routes``; without the file — the
default set, ``lmost link routes <name>`` prints the active one). One line, one rule ``<condition> [args] -> <role>``;
matches add up:

    dm -> self                         a direct message to the role's bot
    mention -> self                    the role's bot is mentioned
    mention @owner <channel>|* -> R    the role's people (OWNER, the role's CO_OWNERS) mentioned at the top level
    mention @<login> <channel>|* -> R
    top <channel> -> R                 a person's top-level post in the channel, except people talking to each other
    thread -> self                     a thread whose root the role's bot wrote or that reached the role by a rule above

The default set: ``dm``, ``mention``, ``top <channel> -> <role>`` for each ``CHANNEL_OWNERS=<channel>:<role>``, ``thread``.
Someone else's thread reaches the role only by a mention, or in full after ``lmost inbox follow``; ``mute`` silences a
thread except mentions. A post by another bot (a colleague's agent) — only when it mentions the role's bot, as ``peer``;
other bots' posts (buttons, CI, automation) — never. Not delivered: own posts, system posts, webhook posts, progress posts,
and everything after ``LMOST_LINK_LOOP_LIMIT`` (6) agent posts in a row in a thread without a person — then "⏸ waiting for
a person" (not in ``LOOP_FREE_CHANNELS``).

**Receipts** — reactions of the addressee's bot: 📬 in the inbox (⌨️ — the role is in CLI mode), 👀 read (inbox ack),
⚠️ an owner's post reached nobody where an agent was expected, or mentions a ``@name`` that does not exist.
**Owner's reactions** on a role's post → ``reaction`` event (👍 yes · ❌ no · 👀 seen).
**Answers** — button marks ``lmost_answers`` on own bots' posts (the edge writes them) → ``answer`` event to the asking
session, or to the answer sink of the question. **Command posts** of the edge (``lmost_command``) → verified with the
owner's key, authority checked, then a bridge handler or a ``command`` event (commands spec).
**Relay** (``RELAY``) — reads only its DM with the owner; a reply in the thread of a relay post goes to the session that
started the thread (``props.agent`` of the root), and to nobody else.

Extension points: ``TICK_HOOKS`` (after each pass of a connection), ``POST_HOOKS`` (each own-bot post with trusted props),
``ANSWER_SINKS``, ``BRIDGE_COMMANDS``.

State — ``<state>/link/``: ``<conn>.<role>.json`` (since, seen, reactions), ``<conn>.answers.json``,
``<conn>.delivered.json``, ``<conn>.unrouted.json``, ``<conn>.commands.json``, ``<conn>.handles.json``.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import threading
import time
import urllib.error
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import activity, askstore, commands, connections, heartbeat, inbox, mattermost, paths, questions, ws

Connection = connections.Connection

LOOP_LIMIT = int(os.environ.get("LMOST_LINK_LOOP_LIMIT", "6"))
EVERY_S = float(os.environ.get("LMOST_LINK_EVERY_S", "3"))
FULL_S = float(os.environ.get("LMOST_LINK_FULL_S", "60"))
RETRY_S = float(os.environ.get("LMOST_LINK_RETRY_S", "30"))
HEARTBEAT_S = float(os.environ.get("LMOST_LINK_HEARTBEAT_S", "60"))
DELIVERED_S = 24 * 3600
SPECIAL = {"all", "here", "channel"}
WAKE_EVENTS = {"hello", "posted", "post_edited", "post_deleted", "reaction_added", "reaction_removed", "channel_viewed"}
# meaning of the owner's reactions on a role's post; others go as they are
MEANING = {"+1": "yes / agreed", "thumbsup": "yes / agreed", "white_check_mark": "yes / agreed",
           "heavy_check_mark": "yes / agreed", "x": "no", "negative_squared_cross_mark": "no", "-1": "no",
           "thumbsdown": "no", "eyes": "seen"}
# button marks in post props: the bridge trusts them only on posts of its own bots (the author writes a post's props,
# and a colleague's bot could put "answered by the owner" there)
MARK_PROPS = ("lmost_answers", "lmost_waiting_drop", "lmost_waiting_snooze", "lmost_discuss", "lmost_confirm")
READBACK = ("\n↩️ an answer in words — before acting, retell it in the thread by consequences: what changes, what does "
            "not, an example; buttons “✅ right” / “✏️ not so” (`lmost post --thread <thread> --to <who answered> --ask …`)")
COMMAND_SINK = "lmost:command"

# ── extension points ───────────────────────────────────────────────────────────────────────────────────────────────────
TICK_HOOKS: List[Callable[["Link"], None]] = []
POST_HOOKS: List[Callable[["Link", dict, str], None]] = []


class SinkRefused(Exception):
    """An answer sink refused the answer; the message goes into the thread as "⚠️ <sink> refused: …"."""


# sink key → fn(link, post, part, answer) -> text of the ``answer`` event for the asking session
ANSWER_SINKS: Dict[str, Callable[["Link", dict, str, dict], str]] = {}
# (command, sub) → fn(link, role, payload, caller_role) -> reply text in the command post's thread
BRIDGE_COMMANDS: Dict[Tuple[str, str], Callable[["Link", str, dict, str], str]] = {}


def log(m: str) -> None:
    sys.stderr.write("lmost link: " + m + "\n")
    sys.stderr.flush()


def state_dir() -> Path:
    return paths.link_state_dir()


def jload(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def jsave(p: Path, obj) -> None:
    paths.private_dir(p.parent)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False))
    os.replace(tmp, p)


def mentions(p: dict) -> Set[str]:
    """@names of a post, except @all/@here/@channel."""
    return set(re.findall(r"(?<![\w.])@([a-z0-9][a-z0-9._-]*[a-z0-9])", p.get("message") or "", re.I)) - SPECIAL


# ── routes ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
class RoutesError(Exception):
    pass


def parse_routes(text: str, src: str = "routes") -> List[dict]:
    """Parse rules. An error is RoutesError "<file>:<line>: …": a bridge with half its rules is worse than none."""
    rules = []
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        def bad(why: str) -> RoutesError:
            return RoutesError(f"{src}:{n}: {why}: {raw.strip()}")
        if "->" not in line:
            raise bad("no “->”")
        left, role = (x.strip() for x in line.rsplit("->", 1))
        parts = left.split()
        if not parts:
            raise bad("no condition")
        if not role or len(role.split()) != 1:
            raise bad("one role after “->”")
        cond, r = parts[0], {"cond": parts[0], "role": role, "n": n, "line": line}
        if cond in ("dm", "thread") or (cond == "mention" and len(parts) == 1):
            if len(parts) != 1:
                raise bad(f"{cond} takes no arguments")
            if role != "self":
                raise bad(f"{cond} is only for the bot's own role: -> self")
        elif cond == "mention":
            if len(parts) != 3 or not parts[1].startswith("@") or len(parts[1]) < 2:
                raise bad("mention @<login>|@owner <channel>|* -> <role>")
            if role == "self":
                raise bad("mention @… takes a role name, not self")
            r.update(person=parts[1][1:], chan=parts[2])
        elif cond == "top":
            if len(parts) != 2:
                raise bad("top <channel> -> <role>")
            if role == "self":
                raise bad("top takes a role name, not self")
            r["chan"] = parts[1]
        else:
            raise bad(f"unknown condition “{cond}” (there are: dm, mention, top, thread)")
        rules.append(r)
    return rules


def default_routes(c: Connection) -> str:
    pairs = [x.split(":", 1) for x in c.get("CHANNEL_OWNERS", "").replace(" ", "").split(",") if ":" in x]
    return "\n".join(["dm -> self", "mention -> self"] + [f"top {ch} -> {r}" for ch, r in pairs]
                     + ["thread -> self"]) + "\n"


def routes_path(c: Connection) -> Path:
    return paths.config_dir() / "connections" / f"{c['NAME']}.routes"


def load_routes(c: Connection) -> Tuple[List[dict], Optional[Path]]:
    p = routes_path(c)
    if p.exists():
        return parse_routes(p.read_text(encoding="utf-8"), str(p)), p
    return parse_routes(default_routes(c), "default set"), None


# ── receipts ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
def receipt(role: str) -> str:
    try:
        mode = inbox.mode_of(role)[0]
    except Exception:
        mode = "chat"
    return "keyboard" if mode == "cli" else "mailbox_with_mail"


def thread_subs(role: str) -> Dict[str, Set[str]]:
    out = {}
    for k in ("mute", "follow"):
        try:
            out[k] = {x.strip() for x in (paths.inbox_dir() / f"{role}.{k}").read_text().splitlines() if x.strip()}
        except OSError:
            out[k] = set()
    return out


class Link:
    """One connection: its role bots, their pass over channels, and what the pass sees."""

    def __init__(self, c: Connection):
        self.c, self.name = c, c["NAME"]
        self.relay = c.get("RELAY", "").strip()
        self.bots = {n: t for n, t in connections.bots(c).items() if connections.admitted(c, n) or n == self.relay}
        self.ids: Dict[str, str] = {}
        self.handle: Dict[str, str] = {}
        for n, t in self.bots.items():
            me = mattermost.api(c, "GET", "/users/me", token=t)
            self.ids[n], self.handle[n] = me["id"], me.get("username") or n
        try:
            jsave(state_dir() / f"{self.name}.handles.json", self.handle)
        except OSError:
            pass
        self.owner: Optional[str] = None
        if self.bots:
            try:
                self.owner = self.api("GET", f"/users/username/{c['OWNER']}")["id"]
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                raise SystemExit(f"lmost link: [{self.name}] OWNER={c.get('OWNER')} is not on the server — renamed? "
                                 f"Fix OWNER in connections/{self.name}.env and restart the bridge")
        self.users: Dict[str, dict] = {}
        self.threads: Dict[str, List[dict]] = {}
        self.paused: Set[str] = set()
        self.loop_free = {x.strip() for x in c.get("LOOP_FREE_CHANNELS", "").split(",") if x.strip()}
        self.routes, self.routes_src = load_routes(c)
        self.check_routes()
        sd = state_dir()
        self.answers_path = sd / f"{self.name}.answers.json"
        self.answered = set(jload(self.answers_path, []))
        self.unrouted_path = sd / f"{self.name}.unrouted.json"
        self.unrouted = jload(self.unrouted_path, {})
        self.delivered_path = sd / f"{self.name}.delivered.json"
        self.delivered = {k: v for k, v in jload(self.delivered_path, {}).items() if v > time.time() - DELIVERED_S}
        self.commands_path = sd / f"{self.name}.commands.json"
        self.commands = jload(self.commands_path, {"done": [], "asked": {}})
        self.full_end = 0.0
        self.beat_at = 0.0
        self.watchers: List[ws.Watcher] = []

    # ── basics ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def api(self, method: str, path: str, body: object = None, who: Optional[str] = None):
        tok = self.bots[who] if who else next(iter(self.bots.values()))
        return mattermost.api(self.c, method, path, body, token=tok)

    def user(self, uid: str) -> dict:
        if uid not in self.users:
            self.users[uid] = self.api("GET", f"/users/{uid}")
        return self.users[uid]

    def put(self, to: str, text: str, **kw) -> None:
        inbox.Inbox(to).put(text, via=self.name, **kw)

    def owner_ids(self, who: Optional[str]) -> Set[str]:
        """The owner and the role's co-owners (CO_OWNERS) — chat ids; cached."""
        cache = self.__dict__.setdefault("co_ids", {})
        if who not in cache:
            ids = {self.owner}
            for login in [x for x in connections.owners(self.c, who or "") if x != self.c.get("OWNER")]:
                try:
                    ids.add(self.api("GET", f"/users/username/{login}")["id"])
                except Exception as e:
                    log(f"[{self.name}] CO_OWNERS {who}:{login} not found: {e}")
            cache[who] = ids
        return cache[who]

    def role(self, uid: str, who: Optional[str] = None) -> str:
        if uid == self.owner or (who and uid in self.owner_ids(who)):
            return "owner"
        if uid in self.ids.values():
            return "peer"
        return "bot" if self.user(uid).get("is_bot") else "person"

    def thread(self, root: str, who: str) -> List[dict]:
        if root not in self.threads:
            r = self.api("GET", f"/posts/{root}/thread", who=who)
            self.threads[root] = sorted(r["posts"].values(), key=lambda x: x["create_at"])
        return self.threads[root]

    def react(self, post_id: str, emoji: str, who: str) -> None:
        """A receipt by the role's bot; a failing receipt never breaks delivery."""
        try:
            self.api("POST", "/reactions", {"user_id": self.ids[who], "post_id": post_id, "emoji_name": emoji}, who=who)
        except Exception as e:
            log(f"[{self.name}] reaction {emoji} on {post_id[:6]}: {e}")

    def reply(self, p: dict, message: str, who: str, **props) -> None:
        body = {"channel_id": p["channel_id"], "root_id": p.get("root_id") or p["id"], "message": message}
        if props:
            body["props"] = props
        self.api("POST", "/posts", body, who=who)

    # ── routes ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def check_routes(self) -> None:
        """A role not admitted or without a bot, a channel missing from the team — a warning, the rule is off."""
        chans: Dict[str, bool] = {}
        for r in self.routes:
            role = r.get("role")
            if role and role != "self" and role not in self.bots:
                r["off"] = f"role {role} is not admitted by the connection or has no bot"
            elif r.get("chan") and r["chan"] != "*":
                if r["chan"] not in chans:
                    try:
                        self.api("GET", f"/teams/name/{self.c['TEAM']}/channels/name/{r['chan']}")
                        chans[r["chan"]] = True
                    except urllib.error.HTTPError as e:
                        chans[r["chan"]] = e.code != 404
                    except Exception:
                        chans[r["chan"]] = True        # a network failure is no reason to switch a rule off
                if not chans[r["chan"]]:
                    r["off"] = f"channel {r['chan']} is not in the team"
            if r.get("off"):
                log(f"[{self.name}] rule “{r['line']}” is off: {r['off']}")

    def rules(self) -> List[dict]:
        return [r for r in self.routes if not r.get("off")]

    def humans(self, names: Set[str]) -> Set[str]:
        """Of the logins — living people (not our bots, not other bots). An API failure counts as a person: quieter."""
        cache = self.__dict__.setdefault("is_human", {})
        out = set()
        for n in names:
            if n in self.handle.values():
                continue
            if n not in cache:
                try:
                    cache[n] = not self.api("GET", f"/users/username/{n}").get("is_bot")
                except urllib.error.HTTPError as e:
                    cache[n] = e.code != 404
                except Exception:
                    cache[n] = True
            if cache[n]:
                out.add(n)
        return out

    def person_set(self, r: dict) -> Set[str]:
        return set(connections.owners(self.c, r["role"])) if r["person"] == "owner" else {r["person"]}

    def match(self, p: dict, who: str, ch: dict, rules: List[dict], top_only: bool = False) -> Optional[str]:
        """The first rule that matched for role ``who`` (the reason as a word) or None. top_only — no ``thread``."""
        m, chn, root = mentions(p), ch.get("name") or "", p.get("root_id")
        ours = set(self.ids.values())
        for r in rules:
            role, cond = r.get("role"), r["cond"]
            if role != "self" and role != who:
                continue
            if cond == "mention" and "person" not in r and self.handle[who] in m:
                return "mention"
            if cond == "dm" and ch.get("type") == "D":
                return "dm"
            # a representative — a person's top-level post; roles' bots mentioning the owner, and people's threads, do
            # not wake the role
            if cond == "mention" and "person" in r and ch.get("type") != "D" and r["chan"] in ("*", chn) and not root \
                    and p.get("user_id") not in ours:
                hit = self.person_set(r) & m
                if r["chan"] == "*":   # `mention @x <channel>` overrides `mention @x *` in that channel
                    hit -= {x for q in rules if q["cond"] == "mention" and q.get("chan") == chn
                            for x in self.person_set(q)}
                if hit:
                    return "representative"
            if cond == "top" and not root and r["chan"] == chn and ch.get("type") != "D" and p.get("user_id") not in ours:
                others = self.humans(m - set(connections.owners(self.c, who)))
                if not others or self.handle[who] in m:
                    return "channel"
            if cond == "thread" and root and not top_only:
                top = next((x for x in self.thread(root, who) if x["id"] == root), None)
                if top and (top.get("user_id") == self.ids[who] or self.match(top, who, ch, rules, top_only=True)):
                    return "thread"
        return None

    def relevant(self, p: dict, who: str, ch: dict) -> Optional[str]:
        rules = self.rules()
        if self.handle[who] in mentions(p) and any(r["cond"] == "mention" and "person" not in r for r in rules):
            return "mention"
        root = p.get("root_id")
        subs = thread_subs(who) if root else {"mute": set(), "follow": set()}
        if root and root in subs["mute"]:
            return None
        why = self.match(p, who, ch, rules)
        if why:
            return why
        if root and root in subs["follow"]:
            return "thread (follow)"
        return None

    def foreign_bot(self, p: dict, who: str) -> Optional[str]:
        return "peer" if self.handle[who] in mentions(p) else None

    # ── loop guard ─────────────────────────────────────────────────────────────────────────────────────────────────
    def owner_touched(self, x: dict) -> bool:
        """A person took part without a post: the owner's button answer or confirmation, or reaction."""
        pr = x.get("props") or {}
        if any((a or {}).get("user_id") == self.owner for a in (pr.get("lmost_answers") or {}).values()):
            return True
        if (pr.get("lmost_confirm") or {}).get("user_id") == self.owner:
            return True
        return any(r.get("user_id") == self.owner for r in (x.get("metadata") or {}).get("reactions") or [])

    def loop_guard(self, p: dict, who: str, ch: Optional[dict] = None) -> bool:
        root = p.get("root_id")
        if not root or (ch or {}).get("name") in self.loop_free:
            return False
        tail = 0
        for x in reversed([x for x in self.thread(root, who) if x["create_at"] <= p["create_at"] and not x.get("type")]):
            if self.role(x["user_id"]) == "peer" and not self.owner_touched(x):
                tail += 1
            else:
                break
        if tail <= LOOP_LIMIT:
            return False
        if root not in self.paused:
            self.paused.add(root)
            self.api("POST", "/posts", {"channel_id": p["channel_id"], "root_id": root,
                                        "message": f"⏸ waiting for a person — {LOOP_LIMIT} agent posts in a row "
                                                   "without one"}, who=who)
            log(f"[{self.name}] thread {root[:6]}: loop guard")
        return True

    # ── reactions ──────────────────────────────────────────────────────────────────────────────────────────────────
    def reaction_target(self, p: dict, who: str) -> Optional[str]:
        """Whose reaction: the session that wrote the post (``props.agent``, when it posts with this bot), the thread's
        session by THREAD_ROUTE, else the role itself; on the relay — only the session of ``props.agent``."""
        agent = (p.get("props") or {}).get("agent")
        if who == self.relay:
            return agent if agent and connections.admitted(self.c, agent) else None
        if not p.get("root_id"):
            route = connections.thread_route(self.c, who, p["id"])
            if route:
                return route
        if agent and connections.admitted(self.c, agent) and connections.bot(self.c, agent) == self.bots.get(who):
            return agent
        return (connections.thread_route(self.c, who, p["root_id"]) if p.get("root_id") else None) or who

    def on_reactions(self, p: dict, who: str, st: dict, ch: dict, mentioned: bool = False) -> None:
        mine = {r["emoji_name"] for r in (p.get("metadata") or {}).get("reactions") or [] if r.get("user_id") == self.owner}
        seen = set(st.setdefault("rx", {}).get(p["id"], []))
        if not mine - seen:
            return
        if mentioned:
            to = connections.thread_route(self.c, who, p.get("root_id") or p["id"]) or who
        else:
            to = self.reaction_target(p, who)
        if not to:
            return
        text = (p.get("message") or "").split("\n")[0][:120]
        chn = "dm" if ch.get("type") == "D" else ch.get("name", "")
        whose = "a post that mentions you" if mentioned else "your post"
        for e in sorted(mine - seen):
            mean = MEANING.get(e, "")
            self.put(to, f":{e}:{' — ' + mean if mean else ''} on {whose} “{text}”", sender=self.user(self.owner)["username"],
                     kind="reaction", role="owner", channel=chn, thread=p.get("root_id") or p["id"], post=p["id"])
            log(f"[{self.name}] {to} ← owner's :{e}: on {p['id'][:6]}")
        st["rx"][p["id"]] = sorted(mine | seen)
        if len(st["rx"]) > 500:
            st["rx"] = dict(list(st["rx"].items())[-500:])

    # ── answers ────────────────────────────────────────────────────────────────────────────────────────────────────
    def _answered(self, key: str) -> None:
        self.answered.add(key)
        jsave(self.answers_path, sorted(self.answered)[-5000:])

    def on_answers(self, p: dict, who: str) -> None:
        """Button answers on a question post → the asking session (or the sink); each mark once per connection."""
        props = p.get("props") or {}
        q = str(props.get("lmost_question") or "")
        spec = props.get("lmost_ask") or {}
        asker = props.get("agent")
        if not q.startswith("ask-"):
            return
        sink = spec.get("sink") or ""
        if sink == COMMAND_SINK:
            return self.on_command_answer(p, who)
        allowed = {self.owner} if sink else set(self.owner_ids(asker)) if asker else {self.owner}
        if not sink:
            for login in spec.get("to") or []:
                try:
                    allowed.add(self.api("GET", f"/users/username/{login}", who=who)["id"])
                except Exception:
                    pass
        # a cancelled question does not count: an answer after the cancel goes nowhere. The cancel time — from the post,
        # or from the asker's registry if an old client's redraw lost it
        gone = [(props.get("lmost_cancelled") or {}).get("at")]
        rec = askstore.asks(asker).get(q) if asker else None
        if rec:
            gone.append(rec.get("cancelled_at"))
        gone_at = min([x for x in gone if x], default=None)
        parts = spec.get("parts") or []
        for part, a in sorted((props.get("lmost_answers") or {}).items()):
            key = f"{p['id']}:{part}:{a.get('at')}"
            if key in self.answered or a.get("user_id") not in allowed:
                continue
            self._answered(key)
            if gone_at and (a.get("at") or 0) >= gone_at:
                log(f"[{self.name}] {q} item {part}: the question was cancelled — answer not counted")
                continue
            n = int(part) if str(part).isdigit() else 0
            title = parts[n - 1].get("title", "") if 0 < n <= len(parts) else ""
            if a.get("text"):
                shown = a["text"]
            elif 0 < n <= len(parts) and a.get("option"):
                shown = parts[n - 1]["options"][a["option"] - 1]
            else:
                shown = f"option {a.get('option')}"
            by = self.user(a["user_id"])["username"]
            thread = p.get("root_id") or p["id"]
            if sink:
                fn = ANSWER_SINKS.get(sink)
                try:
                    if not fn:
                        raise SinkRefused("no such answer sink on this machine")
                    text = fn(self, p, str(part), a)
                except SinkRefused as e:
                    self.reply(p, f"⚠️ {sink} refused: {str(e)[:300]}", who)
                    log(f"[{self.name}] {q} item {part}: sink {sink} refused: {e}")
                    continue
                except Exception as e:
                    self.reply(p, f"⚠️ {sink} refused: {type(e).__name__}", who)
                    log(f"[{self.name}] {q} item {part}: sink {sink} failed: {e}")
                    continue
            else:
                text = f"answer to {q} item {part} “{title}”: {shown}" + (READBACK if a.get("text") else "")
            if asker and connections.admitted(self.c, asker):
                role = "owner" if a["user_id"] in self.owner_ids(asker) else "person"
                self.put(asker, text, sender=by, kind="answer", role=role, thread=thread)
            log(f"[{self.name}] {q} item {part} → {asker or '—'}")
        if asker and rec and rec.get("post") == p["id"] and rec.get("state", "open") == "open" \
                and askstore.ask_state(p) == "answered" and not gone_at:
            askstore.ask_record(asker, {"q": q, "state": "answered"})

    # ── command posts (commands spec, the bridge side) ─────────────────────────────────────────────────────────────
    def _commands_save(self) -> None:
        self.commands["done"] = self.commands["done"][-1000:]
        jsave(self.commands_path, self.commands)

    def command_key(self, payload: dict) -> Optional[str]:
        """The owner's key that must have signed the command: EDGE_SECRET (and its id EDGE_KEY_ID when the post has kid)."""
        secret = self.c.get("EDGE_SECRET", "")
        if payload.get("kid") and payload["kid"] != self.c.get("EDGE_KEY_ID"):
            return None
        return secret or None

    def on_command(self, p: dict, who: str) -> None:
        payload = (p.get("props") or {}).get("lmost_command") or {}
        if p["id"] in self.commands["done"] or p["id"] in self.commands["asked"].values():
            return
        if payload.get("bot") != self.ids.get(who):
            return                                       # meant for another bot (seen in a shared DM) — not ours
        key = self.command_key(payload)
        if not key or not commands.verify(payload, key):
            self.commands["done"].append(p["id"])
            self._commands_save()
            log(f"[{self.name}] {who}: command post {p['id'][:6]} with a wrong key — ignored")
            return
        age_s = time.time() - (payload.get("at") or 0) / 1000
        if age_s > commands.COMMAND_TTL_S:
            self.ask_stale(p, who, payload, age_s)
            return
        self.run_command(p, who, payload)

    def run_command(self, p: dict, who: str, payload: dict) -> None:
        self.commands["done"].append(p["id"])
        self._commands_save()
        name, sub, args = payload.get("name") or "", payload.get("sub") or "", list(payload.get("args") or [])
        cmd = commands.subcommands(name).get(sub)
        caller = payload.get("caller") or {}
        if not cmd or cmd.kind == "edge":
            self.reply(p, f"`/{name} {sub}` is not a bridge or session command here.", who)
            return
        role = who
        if cmd.role_arg is not None:
            if len(args) <= cmd.role_arg:
                self.reply(p, f"`/{name} {sub}`: which role?", who)
                return
            role = args[cmd.role_arg]
            if connections.bot_role(self.c, role) != who and role != who:
                self.reply(p, f"@{self.handle[who]} is not the bot of role {role}.", who)
                return
        crole = self.role(caller.get("id") or "", role)
        if crole == "bot":
            crole = "peer"
        if crole not in cmd.who:
            self.reply(p, f"`/{name} {sub}` is not open to you.", who)
            log(f"[{self.name}] /{name} {sub} by @{caller.get('username')} ({crole}) — refused")
            return
        if cmd.kind == "session":
            rest = [a for i, a in enumerate(args) if i != cmd.role_arg]
            text = f"/{name} {sub} " + " ".join(rest)
            self.put(role, text.strip(), sender=caller.get("username") or "", kind="command", role=crole,
                     channel="dm", thread=payload.get("root_id") or p["id"], post=p["id"])
            self.react(p["id"], receipt(role), who)
            log(f"[{self.name}] {role} ← /{name} {sub} from @{caller.get('username')} ({crole})")
            return
        fn = BRIDGE_COMMANDS.get((name, sub))
        try:
            out = fn(self, role, payload, crole) if fn else f"`/{name} {sub}` is not available on this bridge."
        except Exception as e:
            log(f"[{self.name}] /{name} {sub}: {type(e).__name__}: {e}")
            out = commands.FAILED
        self.reply(p, out, who)

    def ask_stale(self, p: dict, who: str, payload: dict, age_s: float) -> None:
        """A command older than COMMAND_TTL is not run silently: the caller is asked in the post's thread."""
        from . import post as postmod
        caller = (payload.get("caller") or {}).get("username") or ""
        shown = " ".join([f"/{payload.get('name')}", payload.get("sub") or ""] + list(payload.get("args") or []))
        mins = int(age_s // 60)
        spec = {"context": f"`{shown.strip()}` is {mins} min old", "parts": [
            {"title": "still needed?", "text": "", "options": ["yes, run it", "no, drop it"]}]}
        try:
            poster = postmod.Poster(who, via=self.name, warn=lambda m: None)
            poster.ask(spec, to=caller, thread=p["id"], sink=COMMAND_SINK)
            q = askstore.open_asks(who)[-1]["q"]
        except Exception as e:
            log(f"[{self.name}] stale command {p['id'][:6]}: not asked ({e})")
            return
        self.commands["asked"][q] = p["id"]
        self._commands_save()
        log(f"[{self.name}] stale command {p['id'][:6]} ({mins} min) — asked @{caller}")

    def on_command_answer(self, p: dict, who: str) -> None:
        props = p.get("props") or {}
        q = props.get("lmost_question")
        cmd_post = self.commands["asked"].get(q)
        if not cmd_post:
            return
        spec = props.get("lmost_ask") or {}
        allowed = set()
        for login in spec.get("to") or []:
            try:
                allowed.add(self.api("GET", f"/users/username/{login}", who=who)["id"])
            except Exception:
                pass
        for part, a in sorted((props.get("lmost_answers") or {}).items()):
            if a.get("user_id") not in allowed:
                continue                                # only the caller's press counts
            del self.commands["asked"][q]
            askstore.ask_record(who, {"q": q, "state": "answered"})
            cp = self.api("GET", f"/posts/{cmd_post}", who=who)
            if a.get("option") == 1:
                self.run_command(cp, who, (cp.get("props") or {}).get("lmost_command") or {})
            else:
                self.commands["done"].append(cmd_post)
                self._commands_save()
                self.reply(cp, "Dropped.", who)
            return

    # ── "not delivered" ────────────────────────────────────────────────────────────────────────────────────────────
    def exists(self, username: str) -> bool:
        try:
            self.api("GET", f"/users/username/{username}")
            return True
        except urllib.error.HTTPError as e:
            return e.code != 404
        except Exception:
            return True

    def expects_agent(self, p: dict, ch: dict) -> bool:
        if ch.get("type") == "D":
            return True
        return not p.get("root_id") and any(r["cond"] == "top" and r["chan"] == ch.get("name") for r in self.rules())

    def warn_unrouted(self) -> None:
        """An owner's post that reached no inbox — ⚠️ instead of silence; the verdict waits for a full pass of all bots
        that ended after the post was seen."""
        for pid, u in list(self.unrouted.items()):
            if u["hit"] or pid in self.delivered:
                del self.unrouted[pid]
                continue
            if u["passes"] < 1 or u.get("at", 0) > self.full_end:
                u["passes"] += 1
                continue
            p = u["p"]
            del self.unrouted[pid]
            named = mentions(p) - set(self.handle.values()) - {self.c.get("OWNER")}
            stray = sorted(n for n in named if not self.exists(n))
            if not stray and (named or not u.get("expect")):
                continue          # addressed to people or other bots, or a people's channel — not a miss
            who = u["who"]
            if who not in self.bots:
                continue
            self.react(pid, "warning", who)
            if stray:
                msg = (f"⚠️ not delivered: {', '.join('@' + n for n in stray)} — not in this chat (agents: "
                       f"{', '.join(sorted(self.handle.values()))})")
                try:
                    self.reply(p, msg, who)
                except Exception as e:
                    log(f"[{self.name}] ⚠️ reply on {pid[:6]}: {e}")
            log(f"[{self.name}] post {pid[:6]}: nobody ⚠️ {' '.join(stray)}")
        jsave(self.unrouted_path, self.unrouted)
        cut = time.time() - DELIVERED_S
        self.delivered = {k: v for k, v in self.delivered.items() if v > cut}
        jsave(self.delivered_path, self.delivered)

    # ── relay ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def on_relay(self, p: dict, who: str, role: str, ch: dict) -> None:
        """The relay's channel: only its DM with the owner; a reply in a thread — to the session of the root."""
        if ch.get("type") != "D" or self.owner not in (ch.get("name") or "").split("__") or role != "owner":
            return
        root = p.get("root_id")
        top = next((x for x in self.thread(root, who) if x["id"] == root), None) if root else None
        to = (top.get("props") or {}).get("agent") if top and top["user_id"] == self.ids[who] \
            and (top.get("props") or {}).get("relay") == who else None
        self.delivered[p["id"]] = time.time()
        if not to or not connections.admitted(self.c, to):
            self.reply(p, "↪️ the relay does not know which session this is for: reply in the thread of that session's "
                          "post (“from session <name>”)", who)
            return
        self.put(to, (p.get("message") or "") + mattermost.describe(p, lambda m, q: self.api(m, q, who=who)),
                 sender=self.user(p["user_id"])["username"], kind="message", role=role, channel="dm", thread=root,
                 post=p["id"])
        self.react(p["id"], receipt(to), who)
        log(f"[{self.name}] {who} → {to} ← @{self.user(p['user_id'])['username']} (relay, thread {root[:6]})")

    # ── the pass ───────────────────────────────────────────────────────────────────────────────────────────────────
    def once(self, only: Optional[Set[str]] = None) -> None:
        """A pass over the bots' posts (``only`` — just these: a websocket event came for them; None — all)."""
        self.threads = {}
        for who in self.bots:
            if only is not None and who not in only:
                continue
            self.pass_bot(who)
        if only is None:
            self.full_end = time.time()
        self.warn_unrouted()
        for fn in list(TICK_HOOKS):
            try:
                fn(self)
            except Exception as e:
                log(f"[{self.name}] tick hook {getattr(fn, '__name__', fn)}: {e}")
        self.heartbeat()

    def pass_bot(self, who: str) -> None:
        sp = state_dir() / f"{self.name}.{who}.json"
        st = jload(sp, {"since": int(time.time() * 1000), "seen": []})
        posts, chans = [], {}
        for ch in self.api("GET", f"/users/{self.ids[who]}/channels", who=who):
            chans[ch["id"]] = ch
            r = self.api("GET", f"/channels/{ch['id']}/posts?since={st['since']}", who=who)
            posts += [r["posts"][i] for i in r.get("order", [])]
        base = st["since"]        # the previous pass's boundary: a post created later is new for this bot
        for p in sorted(posts, key=lambda x: x.get("update_at", x["create_at"])):
            ver = f"{p['id']}:{p.get('update_at', p['create_at'])}"
            if ver in st["seen"] or p.get("delete_at"):
                continue
            st["seen"].append(ver)
            st["since"] = max(st["since"], p.get("update_at", p["create_at"]))
            try:
                self.on_post(p, who, st, chans.get(p["channel_id"], {}), base)
            except Exception as e:
                log(f"[{self.name}] {who}: post {p['id'][:6]}: {type(e).__name__}: {e}")
        st["seen"] = st["seen"][-2000:]
        jsave(sp, st)

    def on_post(self, p: dict, who: str, st: dict, ch: dict, base: int) -> None:
        props = p.get("props") or {}
        own_bot = p.get("user_id") in self.ids.values()
        if not own_bot:
            props = {k: v for k, v in props.items() if k not in MARK_PROPS}
        else:
            if props.get("lmost_answers"):
                self.on_answers(p, who)
            for fn in list(POST_HOOKS):
                try:
                    fn(self, p, who)
                except Exception as e:
                    log(f"[{self.name}] post hook {getattr(fn, '__name__', fn)}: {e}")
        if props.get("lmost_command") and ch.get("type") == "D":
            self.on_command(p, who)
            return
        if p.get("update_at", p["create_at"]) != p["create_at"]:
            if p["user_id"] == self.ids[who]:
                self.on_reactions(p, who, st, ch)
            elif who != self.relay and self.role(p["user_id"], who) == "peer" and self.handle[who] in mentions(p):
                self.on_reactions(p, who, st, ch, mentioned=True)
        # new or edit: "new" is created after the previous pass — update_at also moves on another bot's reaction.
        # An edit that ADDED a mention of this role is a message, once per post and role.
        got = st.setdefault("got", [])
        edit_mention = p["create_at"] <= base and p.get("edit_at") and p["id"] not in got \
            and self.handle[who] in mentions(p)
        if p.get("type") or p["user_id"] == self.ids[who] or props.get("from_webhook") == "true" \
                or props.get(activity.PROGRESS_PROP) or (p["create_at"] <= base and not edit_mention):
            return
        role = self.role(p["user_id"], who)
        if role == "bot":
            role = self.foreign_bot(p, who)
            if not role:
                return
        if who == self.relay:
            self.on_relay(p, who, role, ch)
            return
        why = self.relevant(p, who, ch)
        if why:
            self.delivered[p["id"]] = time.time()
        if role == "owner" and p["id"] not in self.delivered:
            u = self.unrouted.setdefault(p["id"], {"p": p, "hit": False, "passes": 0, "who": who, "expect": False,
                                                   "at": time.time()})
            u["expect"] = u.get("expect") or self.expects_agent(p, ch)
        if not why or self.loop_guard(p, who, ch):
            return
        chn = "dm" if ch.get("type") == "D" else ch.get("name", "")
        to = connections.thread_route(self.c, who, p.get("root_id") or "") or who
        self.put(to, (p.get("message") or "") + mattermost.describe(p, lambda m, q: self.api(m, q, who=who)),
                 sender=self.user(p["user_id"])["username"], kind="message", role=role, channel=chn,
                 thread=p.get("root_id") or p["id"], post=p["id"])
        self.react(p["id"], receipt(to), who)
        got.append(p["id"])
        st["got"] = got[-500:]
        log(f"[{self.name}] {to if to == who else who + ' → ' + to} ← @{self.user(p['user_id'])['username']} "
            f"({role}, {why}{', edit' if edit_mention else ''}, {chn})")

    # ── heartbeat ──────────────────────────────────────────────────────────────────────────────────────────────────
    def heartbeat(self, force: bool = False) -> None:
        """Once a minute per role bot: state, unread count, last chat turn (the edge reads it, status spec)."""
        if not force and time.time() - self.beat_at < HEARTBEAT_S:
            return
        self.beat_at = time.time()
        shown = jload(activity.activity_dir() / "status.json", {})
        for who, tok in self.bots.items():
            if who == self.relay:
                continue
            emoji = (shown.get(f"{self.name}:{who}") or [""])[0]
            state = {"hammer": "running", "raised_hand": "waiting", "black_circle": "not running"}.get(emoji, "idle")
            t = activity.turn(who)
            try:
                heartbeat.write(self.c, tok, who, state, inbox.Inbox(who).count(),
                                int(t["since"] * 1000) if t and t.get("since") else None)
            except Exception as e:
                log(f"[{self.name}] {who}: heartbeat: {e}")


# ── built-in bridge commands ───────────────────────────────────────────────────────────────────────────────────────────
def _subscribe(cmd: str):
    def run(link: Link, role: str, payload: dict, caller_role: str) -> str:
        thread = payload.get("root_id") or ""
        if not thread:
            return f"`/agent {cmd}` works inside a thread — call it there."
        inbox.Inbox(role).subscribe(cmd, thread)
        return (f"{role} is muted in that thread (a mention still reaches it)." if cmd == "mute"
                else f"{role} follows that thread again.")
    return run


BRIDGE_COMMANDS[("agent", "mute")] = _subscribe("mute")
BRIDGE_COMMANDS[("agent", "follow")] = _subscribe("follow")


# ── the daemon ─────────────────────────────────────────────────────────────────────────────────────────────────────────
class Wakeups:
    """Who should fetch posts: (connection, bot) from websocket events; one Event wakes the main loop."""

    def __init__(self):
        self.lock, self.ev, self.dirty = threading.Lock(), threading.Event(), {}

    def mark(self, conn: str, who: str) -> None:
        with self.lock:
            self.dirty.setdefault(conn, set()).add(who)
        self.ev.set()

    def take(self) -> Dict[str, Set[str]]:
        with self.lock:
            d, self.dirty = self.dirty, {}
        self.ev.clear()
        return d


def connect(pending: Dict[str, Connection]) -> List[Link]:
    """Connections that came up; the rest stay in ``pending`` for a retry — after a reboot the chat server may come up
    after the bridge."""
    out = []
    for name, c in list(pending.items()):
        try:
            x = Link(c)
            del pending[name]
            if x.bots:
                out.append(x)
                log(f"connection {name} ({c.get('URL')}): {', '.join(x.bots)}")
            else:
                log(f"[{name}] no role bots — skipped")
        except SystemExit as e:
            del pending[name]
            log(str(e))
        except Exception as e:
            log(f"[{name}] did not come up: {e} — retry in {RETRY_S:g} s")
    return out


def watch(link: Link, wake: Wakeups) -> None:
    if os.environ.get("LMOST_LINK_WS", "on") == "off":
        return
    for who, tok in link.bots.items():
        w = ws.Watcher(link.c["URL"], tok, lambda e, who=who: e in WAKE_EVENTS and wake.mark(link.name, who),
                       log=lambda m, who=who: log(f"[{link.name}] {who}: {m}"), name=f"ws-{link.name}-{who}")
        w.start()
        link.watchers.append(w)


def blind(link: Link) -> bool:
    """A bot without a live websocket: its events do not come, the connection is polled every tick."""
    return not all(getattr(w, "connected", False) for w in link.watchers)


def serve(once: bool = False, stop: Optional[threading.Event] = None) -> int:
    pending = dict(connections.connections())
    for c in pending.values():
        load_routes(c)                       # a broken routes file stops the bridge before the first pass
    links = connect(pending)
    if not links and not pending:
        log(f"no working connection with role bots in {paths.config_dir() / 'connections'}")
        return 1
    wake = Wakeups()
    use_ws = not once and os.environ.get("LMOST_LINK_WS", "on") != "off"
    if use_ws:
        for x in links:
            watch(x, wake)
    retry_at, full_at = time.time() + RETRY_S, 0.0
    stop = stop or threading.Event()
    try:
        while not stop.is_set():
            if pending and time.time() >= retry_at:
                new = connect(pending)
                links += new
                retry_at = time.time() + RETRY_S
                if use_ws:
                    for x in new:
                        watch(x, wake)
            full = not use_ws or time.time() >= full_at
            dirty = wake.take()
            for x in links:
                if not full and x.name not in dirty and not blind(x):
                    continue
                try:
                    x.once(None if full or blind(x) else dirty[x.name])
                except Exception as e:
                    log(f"[{x.name}] pass failed: {e} — retry")
            if full:
                full_at = time.time() + FULL_S
            try:
                activity.tick()
            except Exception as e:
                log(f"activity tick: {e}")
            if once:
                break
            wake.ev.wait(EVERY_S)
    finally:
        for x in links:
            for w in x.watchers:
                w.stop()
    return 0
