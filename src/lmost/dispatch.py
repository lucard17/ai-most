"""Wake dispatcher: wakes the addressees of inboxes (spec: delivery — "Waking an idle or closed session", "A resumed
session is the same session", "The wake log records reading, not launching", session mode).

The delivery hook hands events to a session that is running a turn; the dispatcher covers the rest, every pass:

    live and idle (``claude agents --json``: status idle) → an inter-session message "N new in inbox — /inbox", sent by a
        short ``claude -p`` on a cheap model with SendMessage — the standard path, independent of the terminal;
    live and busy → nothing (the hook delivers), unless the turn has ended (the transcript's last meaningful record is
        the end of a turn) or the transcript has been silent for LMOST_BUSY_QUIET_S (120 s): "busy" is then background
        work after the turn, and the session is woken as idle;
    live and waiting for a person (a permission prompt) → nothing: it needs a person, not a message;
    closed, listed in the wake list → ``claude --bg $LMOST_WAKE_ARGS -n <role> --resume <id> [--model m] "/inbox"`` from
        the session's own directory: the same session, the owner's permission flags;
    closed, not listed or never existed → the owner gets "nobody to wake" in a DM from the role's bot, once per batch.

Wake list (``paths.wake_list()``), one role per line: ``<role>`` · ``<role> --model <m>`` · ``<role> cmd <command…>`` —
an agent outside Claude Code woken by its own command (env LMOST_WAKE_ROLE, LMOST_WAKE_COUNT, LMOST_WAKE_PROMPT; 60 s).

A session is woken at most once per LMOST_WAKE_MIN_S (120 s) and only for inbox lines that arrived since the last wake —
except a closed session with unread events that was last woken by a message (it ended meanwhile): it is resumed, a
repeated resume at most every LMOST_RESUME_RETRY_S (900 s). A session in CLI mode is never woken. A wake that failed to
launch is retried on the next pass; a launched wake is never retried by itself — the log says whether the inbox cursor
moved within LMOST_WAKE_VERIFY_S (300 s). Wakes run in their own systemd scope where a user systemd exists, not inside the
dispatcher's service cgroup (its memory cap would kill the woken session).

State next to the inboxes: ``<role>.woke`` "<lines> <time> [mode]", ``<role>.verify`` "<time> <cursor> <mode>",
``<role>.cli-log``; log — ``dispatch.log``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from . import connections, inbox, paths, turn_origin

try:  # bot status in chat from session states — optional until that module lands
    from . import activity  # type: ignore
except ImportError:  # pragma: no cover
    activity = None  # type: ignore

MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def claude_bin() -> str:
    return os.environ.get("LMOST_CLAUDE_BIN") or "claude"


def log(msg: str) -> None:
    d = paths.private_dir(paths.inbox_dir())
    with open(d / "dispatch.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


# ── wake list ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def wake_entries() -> Dict[str, dict]:
    """role → {"listed": bool (a Claude Code line), "model": str|None, "cmd": str|None}."""
    out: Dict[str, dict] = {}
    try:
        lines = paths.wake_list().read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        w = line.split()
        if not w or w[0].startswith("#"):
            continue
        e = out.setdefault(w[0], {"listed": False, "model": None, "cmd": None})
        if w[1:2] == ["cmd"]:
            if e["cmd"] is None:
                e["cmd"] = line.split(None, 2)[2] if len(w) > 2 else ""
            continue
        e["listed"] = True
        if len(w) == 3 and w[1] == "--model" and MODEL_RE.match(w[2]) and not e["model"]:
            e["model"] = w[2]
    return out


# ── sessions ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def agents_list() -> Optional[list]:
    """``claude agents --json``; [] without Claude Code (only command wakes then), None if it did not answer."""
    try:
        r = subprocess.run([claude_bin(), "agents", "--json"], capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return []
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        a = json.loads(r.stdout or "[]")
    except ValueError:
        return None
    return a if isinstance(a, list) else None


def live(agents: list, name: str) -> List[dict]:
    """Entries of the role that are alive: a finished background session stays listed without a status."""
    return [a for a in agents if isinstance(a, dict) and a.get("name") == name and a.get("status") is not None]


def status(agents: list, name: str) -> str:
    st = [a.get("status") for a in live(agents, name)]
    if not st:
        return "none"
    if "waiting" in st:
        return "waiting"
    return "idle" if "idle" in st else "busy"


def _projects() -> Path:
    return connections._claude_root() / "projects"


def transcript_of(sid: str) -> Optional[Path]:
    hits = sorted(_projects().glob(f"*/{sid}.jsonl")) if sid else []
    return hits[0] if hits else None


def session_id_of(name: str) -> Optional[str]:
    """The newest session with this title."""
    found = []
    for p in connections._find_transcripts(f'"customTitle":"{name}"'):
        try:
            found.append((p.stat().st_mtime, p))
        except OSError:
            pass
    return max(found)[1].stem if found else None


def session_dir(sid: str) -> str:
    """Directory to resume in: ``--resume`` looks the session up in the project of the current directory."""
    p = transcript_of(sid)
    if p:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for _, line in zip(range(50), f):
                    if '"cwd"' in line:
                        try:
                            cwd = json.loads(line).get("cwd")
                        except ValueError:
                            continue
                        if cwd and os.path.isdir(cwd):
                            return cwd
        except OSError:
            pass
    return os.path.expanduser("~")


# ── running a wake ─────────────────────────────────────────────────────────────────────────────────────────────────────
_scope_ok: Optional[bool] = None


def _scope_available() -> bool:
    global _scope_ok
    if os.environ.get("LMOST_WAKE_NO_SCOPE"):
        return False
    if _scope_ok is None:
        _scope_ok = bool(shutil.which("systemd-run")) and subprocess.run(
            ["systemctl", "--user", "show-environment"], capture_output=True).returncode == 0 \
            if shutil.which("systemctl") else False
    return _scope_ok


def wake_run(name: str, argv: List[str], cwd: str, timeout: Optional[float] = None,
             env: Optional[dict] = None) -> int:
    """Run a wake command in its own scope (memory cap LMOST_WAKE_MEM, 4G); returns the exit code (124 on timeout)."""
    if _scope_available():
        unit = f"lmost-wake-{re.sub(r'[^A-Za-z0-9_-]', '_', name)}-{time.time_ns()}"
        argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}",
                "-p", f"MemoryMax={os.environ.get('LMOST_WAKE_MEM', '4G')}", "-p", "MemorySwapMax=0", "--"] + argv
    try:
        with open(paths.inbox_dir() / "dispatch.log", "a") as logf:
            return subprocess.run(argv, cwd=cwd, timeout=timeout, env=env, stdin=subprocess.DEVNULL,
                                  stdout=logf, stderr=logf).returncode
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127


# ── per-role state ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _read(p: Path) -> List[str]:
    try:
        return p.read_text().split()
    except OSError:
        return []


def _write(p: Path, *fields) -> None:
    p.write_text(" ".join(str(x) for x in fields) + "\n")


def armed(box: inbox.Inbox, mode: str) -> None:
    _write(box.dir / f"{box.name}.verify", int(time.time()), box.cursor(), mode)


def verify(box: inbox.Inbox) -> None:
    """Did the wake land: only a moved cursor proves it (a launched ``claude -p`` may be held by the permission mode)."""
    vf = box.dir / f"{box.name}.verify"
    v = _read(vf)
    if not v:
        return
    t0, cur0, mode = int(v[0]), int(v[1]) if len(v) > 1 else 0, v[2] if len(v) > 2 else "?"
    age = int(time.time()) - t0
    if box.cursor() > cur0:
        log(f"wake {box.name} — read within {age} s ({mode})")
        vf.unlink(missing_ok=True)
    elif age >= _env_f("LMOST_WAKE_VERIFY_S", 300):
        log(f"wake {box.name} — NOT read within {age} s ({mode}): the wake did not land")
        vf.unlink(missing_ok=True)


def wake_prompt(name: str, n: int) -> str:
    return (f"{turn_origin.WAKE_PREFIX} {name}: {n} new in the inbox. Read them with `lmost inbox unread {name} --ack` and "
            f"answer in the thread of each event (`LMOST_ROLE={name} lmost post --via <via> --thread <thread> …`).")


def post_argv() -> List[str]:
    b = os.environ.get("LMOST_POST_BIN")
    return shlex.split(b) if b else [sys.executable, "-m", "lmost", "post"]


def orphan(box: inbox.Inbox, n: int, why: str) -> None:
    """Events wait and nobody can be woken — tell the owner in a DM from the role's bot (no bot or connection — log only)."""
    log(f"wake {box.name} ({n}): nobody to wake — {why}")
    via = ""
    for _, e in box._events(max(0, len(box._lines()) - 50), len(box._lines())):
        via = str(e.get("via") or via)
    if not via:
        return
    hint = os.environ.get("LMOST_WAKE_HINT") or "`claude -n {role}` in its directory, or add `{role}` to the wake list"
    text = (f"✋ {box.name}: {n} unread in the inbox, but there is nobody to wake — {why}. Start it: "
            + hint.replace("{role}", box.name) + ".")
    try:
        r = subprocess.run(post_argv() + ["--via", via, "--channel", "dm", text], capture_output=True, text=True,
                           timeout=30, env=dict(os.environ, LMOST_ROLE=box.name))
        ok = r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        log(f"wake {box.name}: the DM to the owner did not go out")


def _mode(name: str) -> str:
    return inbox.mode_of(name)[0]


# ── one pass ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def pass_once() -> None:
    d = paths.inbox_dir()
    if not d.is_dir():
        return
    agents = agents_list()
    if agents is None:
        log("claude agents --json did not answer")
        return
    if agents and activity is not None:
        for fn in ("status_sync", "waiting_notify"):
            try:
                getattr(activity, fn)(agents)
            except Exception:
                pass
    wl = wake_entries()
    for f in sorted(d.glob("*.jsonl")):
        name = f.stem
        try:
            box = inbox.Inbox(name)
        except inbox.InboxError:
            continue
        verify(box)
        n = box.count()
        if n <= 0:
            continue
        _one(box, n, agents, wl.get(name) or {"listed": False, "model": None, "cmd": None})


def _one(box: inbox.Inbox, n: int, agents: list, entry: dict) -> None:
    name = box.name
    # A live role (agentSetting in the transcript) or background session without a label is "chat": its hook labels
    # it on the first event, but an idle one never runs the hook until woken.
    if _mode(name) == "cli" and not (box.dir / f"{name}.mode").exists():
        lv = live(agents, name)
        if lv:
            tp = transcript_of(str(lv[0].get("sessionId") or ""))
            role_session = False
            if tp:
                try:
                    role_session = '"agentSetting":"' in tp.read_text(errors="replace")
                except OSError:
                    pass
            if lv[0].get("kind") == "background" or role_session:
                inbox.set_mode(name, "chat", if_unset=True)
                log(f"{name}: live role or background session without a mode label — chat")
    if _mode(name) == "cli":
        cl = box.dir / f"{name}.cli-log"
        try:
            fresh = time.time() - cl.stat().st_mtime < 3600
        except OSError:
            fresh = False
        if not fresh:
            log(f"{name}: cli, {n} in the inbox — not waking")
            cl.touch()
        return
    total = len(box._lines())
    w = _read(box.dir / f"{name}.woke")
    prev, last, wmode = (int(w[0]) if w else 0), (int(w[1]) if len(w) > 1 else 0), (w[2] if len(w) > 2 else "")
    now = int(time.time())
    woke = box.dir / f"{name}.woke"
    if now - last < _env_f("LMOST_WAKE_MIN_S", 120):
        return
    wcmd = entry.get("cmd")
    st = status(agents, name)
    if total <= prev:
        if not (wcmd is None and st == "none" and entry.get("listed")
                and (wmode != "resume" or now - last >= _env_f("LMOST_RESUME_RETRY_S", 900))):
            return
    if st == "waiting" and wcmd is None:
        return
    if wcmd is not None:
        env = dict(os.environ, LMOST_WAKE_ROLE=name, LMOST_WAKE_COUNT=str(n), LMOST_WAKE_PROMPT=wake_prompt(name, n))
        rc = wake_run(name, ["sh", "-c", wcmd], cwd=os.path.expanduser("~"), timeout=60, env=env)
        if rc == 0:
            log(f"wake {name} ({n}) — own command")
            _write(woke, total, now)
            armed(box, "cmd")
        else:
            log(f"wake {name} — own command failed (code {rc}), retry on the next pass")
            _write(woke, prev, now)
        return
    if st == "busy":
        sid = str((live(agents, name) or [{}])[0].get("sessionId") or "")
        tp = transcript_of(sid)
        age = now - int(tp.stat().st_mtime) if tp else 0
        if tp and turn_origin.turn_ended(str(tp)):
            st = "idle"
            log(f"{name}: busy, but the turn has ended — waking by message")
        elif age >= _env_f("LMOST_BUSY_QUIET_S", 120):
            st = "idle"
            log(f"{name}: busy, but the transcript has been silent for {age} s — waking by message")
    if st == "busy":
        return
    if st == "idle":
        model = os.environ.get("LMOST_WAKE_MODEL", "haiku")
        prompt = (f"Load tools via ToolSearch 'select:SendMessage,ListAgents'. Send exactly one SendMessage to the local "
                  f"session named '{name}' with text: '{turn_origin.WAKE_PREFIX} {name}: {n} new in inbox — /inbox'. "
                  "Reply only 'done' or the error.")
        rc = wake_run(name, [claude_bin(), "-p", "--model", model, "--permission-mode", "bypassPermissions", prompt],
                      cwd=paths.private_dir(paths.state_dir() / "wake").as_posix(), timeout=120)
        if rc != 0:
            log(f"wake {name} ({n}) — claude -p failed, retry on the next pass")
            _write(woke, prev, now)
            return
        log(f"wake {name} ({n}) — message")
        _write(woke, total, now)
        armed(box, "message")
        return
    # nobody alive: resume from the wake list, or tell the owner
    sid = session_id_of(name) if entry.get("listed") else None
    if not sid:
        why = "no session with this name exists" if entry.get("listed") else "it is not in the wake list"
        orphan(box, n, why)
        _write(woke, total, now, "orphan")
        return
    wd = session_dir(sid)
    settings = ""
    if os.environ.get("LMOST_WAKE_SETTINGS"):
        try:
            r = subprocess.run(shlex.split(os.environ["LMOST_WAKE_SETTINGS"]) + [name], capture_output=True,
                               text=True, timeout=10)
            settings = r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            settings = ""
    argv = [claude_bin(), "--bg"] + shlex.split(os.environ.get("LMOST_WAKE_ARGS", "")) + ["-n", name]
    if settings:
        argv += ["--settings", settings]
    argv += ["--resume", sid]
    if entry.get("model"):
        argv += ["--model", entry["model"]]
    argv.append("/inbox")
    if wake_run(name, argv, cwd=wd) == 0:
        log(f"wake {name} ({n}) — --bg --resume {sid[:8]}")
    else:
        log(f"wake {name} — --bg --resume failed")
    _write(woke, total, now, "resume")
    armed(box, "resume")


def serve() -> None:
    every = _env_f("LMOST_DISPATCH_EVERY_S", 5)
    while True:
        try:
            pass_once()
        except Exception as e:  # one bad pass must not stop the service
            try:
                log(f"pass failed: {type(e).__name__}: {e}")
            except OSError:
                pass
        time.sleep(every)
