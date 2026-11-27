"""From the chat one can see that an agent is alive and what it is doing (status spec). The session does nothing for it:
the inbox, the hooks, the bridge and the dispatcher call in here.

A *chat turn* starts when the session reads a person's message (or button answer) from the chat (inbox ack →
``begin``) and ends at the end of a turn with no new events (Stop hook → ``end``). While it runs:

* "typing…" in the message's thread — ``tick`` from the bridge loop; stops after ``LMOST_ACTIVITY_QUIET_S`` (600 s)
  without steps or when the turn is older than ``LMOST_ACTIVITY_MAX_S`` (3600 s);
* a progress post — only in a direct message: after ``LMOST_PROGRESS_AFTER_S`` (20 s) the latest steps (descriptions of
  the session's tool calls, PostToolUse → ``step``), time, step count; edited at most every ``LMOST_PROGRESS_EVERY_S``
  (8 s); at the end "✅ in N min · M steps". ``PROGRESS=off`` on the connection — no post at all.
  ``PROGRESS_STEPS`` = dm (default) | all | none — where the step lines are shown;
* reactions on the post that woke the turn: 🔨 begin, ✋ when it stops on a person, ✅ end; they pile up, never removed.

Bot status (custom status) — ``status_sync`` with ``claude agents --json``: 🔨 "working: <start of the request>",
✋ "waiting for permission", idle — no status, ⚫ "not running"; written only on change.
``waiting_notify`` — a session waiting for a person longer than ``LMOST_WAIT_NOTIFY_AFTER_S`` (120 s) gets one direct
message to the owner, repeated at most every ``LMOST_WAIT_NOTIFY_REPEAT_S`` (1800 s); the bot is the session's own,
else ``WAIT_NOTIFY_AS`` of the connection.

State — ``LMOST_ACTIVITY_DIR`` (``<state>/activity``, 0700): ``<role>.json`` — the running turn, ``status.json``,
``waiting.json``. API failures never break the caller.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional

from . import connections, mattermost, paths

SHOW = 6
SERVICE_FILES = ("status.json", "waiting.json")
PROGRESS_PROP = "lmost_progress"


def _f(var: str, default: str) -> float:
    return float(os.environ.get(var, default))


def activity_dir() -> Path:
    return Path(os.environ.get("LMOST_ACTIVITY_DIR") or paths.state_dir() / "activity")


def _path(role: str) -> Path:
    return activity_dir() / (role + ".json")


def log(msg: str) -> None:
    sys.stderr.write("lmost activity: " + msg + "\n")


def turn(role: str) -> Optional[dict]:
    """The role's running chat turn, or None (read by ``lmost post`` to default into the turn's thread)."""
    try:
        st = json.loads(_path(role).read_text())
    except (OSError, ValueError):
        return None
    return st if st and not st.get("ended") else None


def mins(s: float) -> str:
    s = int(s)
    return f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"


class Locked:
    """The turn file under flock: ``step`` runs from a hook on every tool call, ``tick`` from the bridge."""

    def __init__(self, role: str):
        self.role = role
        self.st: Optional[dict] = None

    def __enter__(self) -> "Locked":
        paths.private_dir(activity_dir())
        self.fd = open(str(_path(self.role)) + ".lock", "w")
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        try:
            self.st = json.loads(_path(self.role).read_text())
        except (OSError, ValueError):
            self.st = None
        return self

    def save(self) -> None:
        _write_json(_path(self.role), self.st)

    def drop(self) -> None:
        try:
            _path(self.role).unlink()
        except FileNotFoundError:
            pass

    def __exit__(self, *a) -> None:
        self.fd.close()


def _write_json(p: Path, data: object) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, p)


def _conn_token(via: str, role: str):
    c = connections.connections().get(via)
    t = connections.bot(c, role) if c else None
    return (c, t) if t else (None, None)


def _api(st: dict, method: str, path: str, body: object = None) -> object:
    c, t = _conn_token(st["via"], st["name"])
    if not c:
        raise RuntimeError(f"no bot for {st['name']} in {st['via']}")
    return mattermost.api(c, method, path, body, token=t)


# ── reactions ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def react_now(via: str, role: str, post: str, emoji: str) -> None:
    c, t = _conn_token(via, role)
    if not c:
        return
    me = mattermost.api(c, "GET", "/users/me", token=t)["id"]
    mattermost.api(c, "POST", "/reactions", {"user_id": me, "post_id": post, "emoji_name": emoji}, token=t)


def react(via: Optional[str], role: str, post: Optional[str], emoji: str) -> None:
    """A reaction of the role's bot; in the background — neither the turn nor the bridge waits for it
    (``LMOST_ACTIVITY_SYNC=1`` — inline, for tests)."""
    if not via or not post:
        return
    if os.environ.get("LMOST_ACTIVITY_SYNC"):
        try:
            react_now(via, role, post, emoji)
        except Exception as e:
            log(f"{role}: reaction: {e}")
        return
    try:
        subprocess.Popen([sys.executable, "-m", "lmost", "activity", "react", via, role, post, emoji],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         env=dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p)))
    except OSError:
        pass


# ── progress post ──────────────────────────────────────────────────────────────────────────────────────────────────────
def render(st: dict, done: bool = False) -> str:
    took = (st.get("ended") or time.time()) - st["since"]
    if done:
        return f"✅ in {mins(took)} · steps {st['n']}"
    lines = [f"⏳ running {mins(took)} · steps {st['n']}"]
    if st.get("show_steps"):
        lines += ["· " + s for s in st["steps"][-SHOW:]]
    return "\n".join(lines)


def _progress_on(st: dict) -> bool:
    return (connections.connections().get(st["via"]) or {}).get("PROGRESS", "on") != "off"


def flush(st: dict, force: bool = False) -> None:
    """Create or edit the progress post when it is time. Only in a direct message: in a channel the outcome and deleted
    progress posts are noise in a shared thread — there "typing…" and the bot status show the turn."""
    now = time.time()
    if not st.get("in_dm") or not _progress_on(st) or (not st.get("dirty") and not force):
        return
    if not st.get("progress") and now - st["since"] < _f("LMOST_PROGRESS_AFTER_S", "20"):
        return
    if st.get("progress") and not force and now - st.get("edited", 0) < _f("LMOST_PROGRESS_EVERY_S", "8"):
        return
    try:
        if not st.get("channel_id"):
            st["channel_id"] = _api(st, "GET", f"/posts/{st['root']}")["channel_id"]
        msg = render(st, done=bool(st.get("ended")))
        if st.get("progress"):
            _api(st, "PUT", f"/posts/{st['progress']}/patch", {"message": msg})
        elif not st.get("ended"):
            st["progress"] = _api(st, "POST", "/posts", {
                "channel_id": st["channel_id"], "root_id": st["root"], "message": msg,
                "props": {"agent": st["name"], PROGRESS_PROP: True}})["id"]
        st["edited"], st["dirty"] = now, False
    except Exception as e:
        log(f"{st['name']}: progress: {e}")


# ── the turn ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def begin(role: str, ev: dict) -> None:
    via, root = ev.get("via"), ev.get("thread") or ev.get("post")
    if not via or not root:
        return
    with Locked(role) as L:
        post = ev.get("post")
        if post:
            react(via, role, post, "hammer")
        if L.st and L.st.get("root") == root and not L.st.get("ended"):
            # the same matter — the turn goes on; the status shows the latest request
            L.st["last"] = time.time()
            L.st["text"] = (ev.get("text") or L.st.get("text") or "")[:200]
            if ev.get("from"):
                L.st["from"], L.st["role"] = ev["from"], ev.get("role")
            if post:
                L.st["posts"] = (L.st.get("posts") or []) + [post]
                L.st.pop("waved", None)
            L.save()
            return
        if L.st and L.st.get("progress") and not L.st.get("ended"):
            L.st["ended"] = time.time()  # a new matter in another thread — close the previous progress post
            flush(L.st, force=True)
        mode = (connections.connections().get(via) or {}).get("PROGRESS_STEPS", "dm")
        in_dm = ev.get("channel") == "dm"
        L.st = {"name": role, "via": via, "root": root, "text": (ev.get("text") or "")[:200], "since": time.time(),
                "last": time.time(), "n": 0, "steps": [], "dirty": False,
                "show_steps": mode == "all" or (mode == "dm" and in_dm), "in_dm": in_dm,
                "posts": [post] if post else [], "from": ev.get("from") or "", "role": ev.get("role") or ""}
        L.save()


def on_ack(role: str, events: List[dict]) -> None:
    """Inbox ack listener: the last person's message or button answer from the chat starts (or continues) a turn."""
    picked = [e for e in events if e.get("role") in ("owner", "person") and e.get("via")
              and (e.get("kind") or "message") in ("message", "answer")]
    if picked:
        begin(role, picked[-1])


def step(role: str, desc: str = "") -> None:
    with Locked(role) as L:
        if not L.st or L.st.get("ended"):
            return
        L.st["n"] += 1
        L.st["last"] = time.time()
        desc = " ".join((desc or "").split())[:120]
        if desc:
            L.st["steps"] = (L.st["steps"] + [desc])[-SHOW:]
            L.st["dirty"] = True
        flush(L.st)
        L.save()


def end(role: str) -> None:
    with Locked(role) as L:
        if not L.st:
            return
        L.st["ended"] = time.time()
        if L.st.get("progress"):
            flush(L.st, force=True)
        for p in L.st.get("posts") or []:
            react(L.st.get("via"), role, p, "white_check_mark")
        L.drop()


def tick() -> None:
    d = activity_dir()
    if not d.is_dir():
        return
    now = time.time()
    for f in sorted(os.listdir(d)):
        if not f.endswith(".json") or f in SERVICE_FILES:
            continue
        with Locked(f[:-5]) as L:
            st = L.st
            if not st or st.get("ended"):
                continue
            if now - st["last"] > _f("LMOST_ACTIVITY_QUIET_S", "600") or now - st["since"] > _f("LMOST_ACTIVITY_MAX_S", "3600"):
                continue  # silence or too long — do not "type" for nothing
            try:
                if not st.get("channel_id"):
                    st["channel_id"] = _api(st, "GET", f"/posts/{st['root']}")["channel_id"]
                if not st.get("uid"):
                    st["uid"] = _api(st, "GET", "/users/me")["id"]
                _api(st, "POST", f"/users/{st['uid']}/typing", {"channel_id": st["channel_id"], "parent_id": st["root"]})
            except Exception as e:
                log(f"{st['name']}: typing: {e}")
            flush(st)
            L.save()


# ── bot status from `claude agents --json` ─────────────────────────────────────────────────────────────────────────────
RANK = {"off": 0, "idle": 1, "busy": 2, "waiting": 3}


def _states(agents: Iterable[dict]):
    """name → off | idle | busy | waiting; waiting wins (a background session on a prompt is seen by nobody)."""
    state, why = {}, {}
    for a in agents:
        n = a.get("name")
        s = a.get("status") if a.get("status") in ("busy", "idle", "waiting") else "off"
        if n and RANK[s] > RANK[state.get(n, "off")]:
            state[n] = s
            if s == "waiting":
                why[n] = a.get("waitingFor") or ""
    return state, why


def status_of(role: str, s: str, waiting_for: str = ""):
    """(emoji, text) of the bot's custom status; ("", "") — none."""
    if s == "idle":
        return "", ""
    if s == "off":
        return "black_circle", "not running"
    if s == "waiting":
        w = waiting_for
        text = "waiting for permission" if "permission" in w else ("waiting: " + w)[:100] if w else "waiting for a person"
        return "raised_hand", text
    t = turn(role)
    if t and t.get("text"):
        return "hammer", "working: " + " ".join(t["text"].split())[:80]
    return "hammer", "working"


def status_sync(agents: List[dict]) -> None:
    state, why = _states(agents)
    paths.private_dir(activity_dir())
    sp = activity_dir() / "status.json"
    try:
        last = json.loads(sp.read_text())
    except (OSError, ValueError):
        last = {}
    cwds = {a.get("name"): a.get("cwd") for a in agents if a.get("name") and a.get("cwd")}
    for cname, c in connections.connections().items():
        for who, tok in connections.bots(c).items():
            if not connections.admitted(c, who, cwds.get(who)):
                continue
            s = state.get(who, "off")
            emoji, text = status_of(who, s, why.get(who, ""))
            if s == "waiting":
                with Locked(who) as L:  # a chat turn stopped on a person — ✋ on its post, once
                    if L.st and not L.st.get("ended") and L.st.get("posts") and not L.st.get("waved"):
                        react(L.st.get("via"), who, L.st["posts"][-1], "raised_hand")
                        L.st["waved"] = True
                        L.save()
            key = f"{cname}:{who}"
            if last.get(key) == [emoji, text]:
                continue
            try:
                if emoji:
                    mattermost.api(c, "PUT", "/users/me/status/custom", {"emoji": emoji, "text": text}, token=tok)
                else:
                    mattermost.api(c, "DELETE", "/users/me/status/custom", token=tok)
                last[key] = [emoji, text]
            except Exception as e:
                log(f"[{cname}] {who}: status: {e}")
    _write_json(sp, last)


# ── a session waiting for a person ─────────────────────────────────────────────────────────────────────────────────────
def last_action(session_id: str) -> str:
    """What the session wants to do: the description (or start of the command) of the last tool call in its transcript."""
    for f in connections._claude_root().glob(f"projects/*/{session_id}.jsonl"):
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        except OSError:
            continue
        for ln in reversed(lines):
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            c = (r.get("message") or {}).get("content") if r.get("type") == "assistant" else None
            for u in reversed(c if isinstance(c, list) else []):
                if isinstance(u, dict) and u.get("type") == "tool_use":
                    i = u.get("input") or {}
                    d = i.get("description") or i.get("command") or i.get("file_path") or ""
                    return f"{u.get('name')}: " + " ".join(str(d).split())[:160]
    return ""


def _dm_owner(c: connections.Connection, tok: str, text: str) -> None:
    me = mattermost.api(c, "GET", "/users/me", token=tok)["id"]
    owner = mattermost.api(c, "GET", f"/users/username/{c['OWNER']}", token=tok)["id"]
    ch = mattermost.api(c, "POST", "/channels/direct", [me, owner], token=tok)["id"]
    mattermost.api(c, "POST", "/posts", {"channel_id": ch, "message": text}, token=tok)


def waiting_notify(agents: List[dict], now: Optional[float] = None) -> None:
    now = now or time.time()
    paths.private_dir(activity_dir())
    sp = activity_dir() / "waiting.json"
    try:
        st = json.loads(sp.read_text())
    except (OSError, ValueError):
        st = {}
    seen = {}
    for a in agents:
        sid, name = a.get("sessionId") or a.get("id"), a.get("name")
        if a.get("status") != "waiting" or not sid or not name:
            continue
        e = st.get(sid) or {"since": now, "told": 0}
        seen[sid] = e
        if now - e["since"] < _f("LMOST_WAIT_NOTIFY_AFTER_S", "120") or now - e["told"] < _f("LMOST_WAIT_NOTIFY_REPEAT_S", "1800"):
            continue
        c = connections.own_connection(name, a.get("cwd"))
        if not c or not c.get("OWNER"):
            continue  # not admitted anywhere — nobody to tell
        who = name if connections.bot(c, name) else c.get("WAIT_NOTIFY_AS", "").strip()
        tok = connections.bot(c, who) if who else None
        if not tok:
            log(f"{name}: waiting, but no bot (neither its own nor WAIT_NOTIFY_AS in {c['NAME']})")
            continue
        w = a.get("waitingFor") or "a person"
        what = last_action(sid)
        text = (f"✋ **{name}** is waiting for {'permission' if 'permission' in w else w} {mins(now - e['since'])}"
                + (f" — `{what}`" if what else "") + f"\nTo answer: `claude agents` → {name}")
        try:
            _dm_owner(c, tok, text)
            e["told"] = now
        except Exception as ex:
            log(f"{name}: waiting — message not sent: {ex}")
    _write_json(sp, seen)
