"""Session inbox: an append-only event queue per role, with a read cursor.

Sources (the link daemon, other sessions, plugins) append events; the delivery hook and the dispatcher hand them to the
session. Storage, all under ``paths.inbox_dir()`` (mode 0700, the trust boundary — only this unix user can write):

    <role>.jsonl    events, one JSON object per line, only appended
    <role>.cursor   how many lines have been handed out
    <role>.lock     flock target: two readers never hand out the same event twice
    <role>.mode     "chat|cli <epoch>" — explicit session mode label
    <role>.mute     thread ids that do not wake the role (a mention still does)
    <role>.follow   foreign thread ids the role gets in full

An event: ``{"at", "from", "kind", "text"}`` plus optional ``thread``, ``channel``, ``via`` (connection), ``role``
(sender role: owner / peer / person), ``post`` (chat post id). Specs: delivery, authority.
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from . import paths

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
THREAD_RE = re.compile(r"^[a-z0-9]{20,32}$")
DEFAULT_URGENT = "answer message reaction discuss"
DEFAULT_EVENT_MAX = 1000
# Marker the questions module puts into a free-text answer; such an answer counts as the owner's words.
FREE_TEXT_MARK = "↩️ free-text answer"
BOT_CACHE_S = 600

Event = Dict[str, object]


class InboxError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def valid_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise InboxError(f"bad role name {name!r}")
    return name


def is_peer(e: Event) -> bool:
    """A chat post from someone who is not the owner (the peer guard's trigger)."""
    return e.get("role") in ("peer", "person") and bool(e.get("via"))


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# Called after events are acknowledged: fn(role, acked_events). Receipts (👀) and activity ("typing…") register here.
AckListener = Callable[[str, List[Event]], None]
ACK_LISTENERS: List[AckListener] = []


class Inbox:
    def __init__(self, name: str, directory: Optional[Path] = None):
        self.name = valid_name(name)
        self.dir = paths.private_dir(Path(directory) if directory else paths.inbox_dir())

    # ── files ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def _f(self, suffix: str) -> Path:
        return self.dir / f"{self.name}{suffix}"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with open(self._f(".lock"), "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def _lines(self) -> List[str]:
        try:
            with open(self._f(".jsonl"), encoding="utf-8") as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def cursor(self) -> int:
        try:
            return int(self._f(".cursor").read_text().strip() or 0)
        except (FileNotFoundError, ValueError):
            return 0

    def _set_cursor(self, n: int) -> None:
        tmp = self._f(".cursor.tmp")
        tmp.write_text(f"{n}\n")
        os.replace(tmp, self._f(".cursor"))

    def _events(self, start: int, end: int) -> List[Tuple[int, Event]]:
        """Events with 1-based line numbers (the number ``show`` takes)."""
        out = []
        for i, line in enumerate(self._lines()[start:end], start + 1):
            try:
                out.append((i, json.loads(line)))
            except json.JSONDecodeError:
                out.append((i, {"kind": "broken", "text": line}))
        return out

    # ── writing ────────────────────────────────────────────────────────────────────────────────────────────────────
    def put(self, text: str, *, sender: str = "", kind: str = "message", via: str = "", role: str = "",
            channel: str = "", thread: str = "", post: str = "") -> Event:
        if not text:
            raise InboxError("empty text")
        e: Event = {"at": now_iso(), "from": sender, "kind": kind or "message", "text": text}
        for k, v in (("thread", thread), ("channel", channel), ("via", via), ("role", role), ("post", post)):
            if v:
                e[k] = v
        with self._locked():
            with open(self._f(".jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.chmod(self._f(".jsonl"), 0o600)
        return e

    # ── reading ────────────────────────────────────────────────────────────────────────────────────────────────────
    def count(self) -> int:
        return max(len(self._lines()) - self.cursor(), 0)

    def unread(self, ack: bool = False, before_peer: bool = False) -> List[Tuple[int, Event]]:
        """Unread events; ``ack`` moves the cursor past them. ``before_peer`` stops at the first peer post: the cursor is
        linear, so a peer post and everything after it wait for the end of the owner's turn."""
        with self._locked():
            c, n = self.cursor(), len(self._lines())
            if n <= c:
                return []
            events = self._events(c, n)
            if before_peer:
                k = next((j for j, (_, e) in enumerate(events) if is_peer(e)), -1)
                if k >= 0:
                    events = events[:k]
                if not events:
                    return []
            if ack:
                self._set_cursor(c + len(events))
        if ack:
            acked = [e for _, e in events]
            write_turn_marks(self.name, acked)
            for fn in list(ACK_LISTENERS):
                try:
                    fn(self.name, acked)
                except Exception:  # a listener (receipt, activity) must never break delivery
                    pass
        return events

    def urgent(self, kinds: Optional[Sequence[str]] = None) -> bool:
        kinds = list(kinds) if kinds is not None else os.environ.get("LMOST_URGENT_KINDS", DEFAULT_URGENT).split()
        c = self.cursor()
        return any((e.get("kind") or "message") in kinds for _, e in self._events(c, len(self._lines())))

    def peer_pending(self) -> Optional[Tuple[int, List[str]]]:
        c = self.cursor()
        peers = [e for _, e in self._events(c, len(self._lines())) if is_peer(e)]
        if not peers:
            return None
        return len(peers), sorted({str(e.get("from") or "") for e in peers})

    def show(self, i: int) -> Optional[Event]:
        lines = self._lines()
        if not 1 <= i <= len(lines):
            return None
        return json.loads(lines[i - 1])

    # ── thread subscriptions ───────────────────────────────────────────────────────────────────────────────────────
    def subscribe(self, cmd: str, thread: str) -> None:
        """mute — the thread does not wake the role (a mention still does); follow — get a foreign thread in full.
        The two lists exclude each other; un* removes from its list."""
        if cmd not in ("mute", "unmute", "follow", "unfollow"):
            raise InboxError(f"unknown command {cmd!r}")
        if not THREAD_RE.match(thread or ""):
            raise InboxError("thread id?")
        base = cmd[2:] if cmd.startswith("un") else cmd
        own = self._f("." + base)
        other = self._f(".follow" if base == "mute" else ".mute")
        for p in (own, other):
            p.touch(exist_ok=True)

        def ids(p: Path) -> List[str]:
            return [x for x in p.read_text().splitlines() if x]

        if cmd.startswith("un"):
            own.write_text("".join(f"{x}\n" for x in ids(own) if x != thread))
        else:
            if thread not in ids(own):
                with open(own, "a") as f:
                    f.write(thread + "\n")
            other.write_text("".join(f"{x}\n" for x in ids(other) if x != thread))

    def threads(self, kind: str) -> List[str]:
        try:
            return [x for x in self._f("." + kind).read_text().splitlines() if x]
        except FileNotFoundError:
            return []


# ── turn marks (read by the guard hooks) ───────────────────────────────────────────────────────────────────────────────
def write_turn_marks(role: str, acked: List[Event]) -> None:
    """Marks for the guards, from the events just handed out:
    <role>.chat        the owner's chat message — the turn is "from chat" (confirmation guard);
    <role>.owner-words the owner spoke not by a button (stale-questions reminder);
    <role>.peer        a peer's chat post — the turn is read-only (peer guard). Set even when the batch also holds the
                       owner's message: the cursor is linear, an owner's message in any thread must not clear it."""
    owner = [e for e in acked if e.get("role") == "owner" and e.get("via")]
    peers = sorted({str(e.get("from") or "") for e in acked if is_peer(e)})
    md = None

    def mark(suffix: str, content: str) -> None:
        nonlocal md
        md = md or paths.private_dir(paths.marks_dir())
        (md / f"{role}{suffix}").write_text(content + "\n")

    if any(e.get("kind") == "message" for e in owner):
        mark(".chat", str(int(time.time())))
    if any(e.get("kind") == "message" or (e.get("kind") == "answer" and FREE_TEXT_MARK in str(e.get("text") or ""))
           for e in owner):
        mark(".owner-words", str(int(time.time())))
    if peers:
        mark(".peer", ", ".join(peers))


# ── formatting ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def handles() -> Dict[str, str]:
    """role → its bot's username, from the link daemon's state (a role named ``hash`` may post as ``@me-hash``)."""
    out: Dict[str, str] = {}
    for p in sorted(glob.glob(str(paths.link_state_dir() / "*.handles.json"))):
        try:
            out.update(json.loads(Path(p).read_text()))
        except (OSError, ValueError):
            pass
    return out


def format_event(i: int, e: Event, role: str, known: Dict[str, str], max_len: int = DEFAULT_EVENT_MAX) -> str:
    t = e.get("text")
    if t is None:
        t = e.get("answer")
    if t is None:
        t = json.dumps(e, ensure_ascii=False, separators=(",", ":"))
    t = str(t)
    sender = str(e.get("from") or "")
    s = f"· #{i} {str(e.get('at') or '')[11:16]} {sender or '?'}"
    h = known.get(sender) if sender else None
    if h and h != sender:
        s += f" (@{h})"
    s += f" [{e.get('kind') or 'message'}]"
    if e.get("role"):
        s += f" ({e['role']})"
    for k, label in (("via", "via"), ("channel", "channel"), ("thread", "thread")):
        if e.get(k):
            s += f" {label}={e[k]}"
    if e.get("q"):
        s += f" {e['q']}" + (f" part {e['part']}" if e.get("part") else "")
    if len(t) > max_len:
        t = f"{t[:max_len]} … (+{len(t) - max_len} chars; full: lmost inbox show {role} {i})"
    return f"{s}: {t}"


# ── session mode ───────────────────────────────────────────────────────────────────────────────────────────────────────
def _has_bot_default(role: str) -> bool:
    try:
        from . import connections  # ported later; until then nobody has a bot
    except ImportError:
        return False
    return connections.has_bot(role)


def mode_of(role: str, directory: Optional[Path] = None,
            has_bot: Callable[[str], bool] = _has_bot_default) -> Tuple[str, str]:
    """(mode, source). chat — the inbox wakes the session and delivers mid-turn, questions go to chat; cli — the session
    is cut off: no waking, events wait for /inbox, questions in the terminal. An explicit label wins; without one —
    chat if the role is in the wake list or has its own bot, else cli."""
    d = Path(directory) if directory else paths.inbox_dir()
    try:
        m, *rest = (d / f"{valid_name(role)}.mode").read_text().split()
        if m in ("chat", "cli"):
            when = ""
            if rest and rest[0].isdigit():
                when = " since " + datetime.fromtimestamp(int(rest[0])).strftime("%d.%m %H:%M")
            return m, f"label{when}"
    except (FileNotFoundError, ValueError):
        pass
    try:
        if any(line.split()[:1] == [role] for line in paths.wake_list().read_text().splitlines()):
            return "chat", "wake list"
    except FileNotFoundError:
        pass
    cache = d / ".modes" / role
    try:
        fresh = time.time() - cache.stat().st_mtime < BOT_CACHE_S
    except FileNotFoundError:
        fresh = False
    if fresh:
        bot = cache.read_text() == "bot"
    else:
        try:
            bot = bool(has_bot(role))
        except Exception:
            bot = False
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("bot" if bot else "nobot")
    return ("chat", "own bot") if bot else ("cli", "default")


def set_mode(role: str, value: str, if_unset: bool = False, directory: Optional[Path] = None) -> None:
    d = paths.private_dir(Path(directory) if directory else paths.inbox_dir())
    f = d / f"{valid_name(role)}.mode"
    if value == "auto":
        f.unlink(missing_ok=True)
        return
    if value not in ("chat", "cli"):
        raise InboxError("mode is cli|chat|auto")
    if if_unset and f.exists():
        return
    tmp = d / f"{role}.mode.tmp"
    tmp.write_text(f"{value} {int(time.time())}\n")
    os.replace(tmp, f)


# ── session name ───────────────────────────────────────────────────────────────────────────────────────────────────────
def session_name(session_id: str = "") -> Optional[str]:
    """The role name: ``LMOST_ROLE`` if set at launch (Codex has no /rename), else the last custom title of the
    Claude Code session's transcript."""
    if os.environ.get("LMOST_ROLE"):
        return os.environ["LMOST_ROLE"]
    sid = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not sid:
        return None
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    hits = sorted(glob.glob(os.path.join(root, "projects", "*", f"{sid}.jsonl")))
    if not hits:
        return None
    title = None
    with open(hits[0], encoding="utf-8") as f:
        for line in f:
            if '"custom-title"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "custom-title" and rec.get("customTitle"):
                title = rec["customTitle"]
    return title


def _default_listeners() -> None:
    from . import activity  # a person's message read from the chat starts a chat turn ("typing…", progress, status)
    ACK_LISTENERS.append(activity.on_ack)
    from . import receipts  # 👀 on the posts the session has read
    ACK_LISTENERS.append(receipts.on_ack)


_default_listeners()
