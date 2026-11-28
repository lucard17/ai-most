"""Connections to chat servers and admission of sessions.

A connection is ``<config>/connections/<name>.env`` (KEY=VALUE, mode 0600). Keys used by the core:

    URL, OWNER, TEAM                    server, the owner's login, team name
    EDGE_SECRET, EDGE_KEY_ID            the owner's button key and its id (edge spec)
    SESSIONS        names or patterns (``review-*``, ``*``), comma-separated — admitted explicitly
    SESSIONS_CWD    absolute directory — sessions started in it or below are admitted
    EXCLUDE         names never admitted (exceptions to SESSIONS_CWD)
    BOT_SHARED      ``<pattern>:<bot role>,…`` — sessions matching the pattern post with that role's bot
    THREAD_ROUTE    ``<bot role>=<command>,…`` — the command maps a thread root id to the session that owns the thread
    RELAY           a relay bot role: an admitted session without a bot posts through it, only to the owner's DM, signed
    CO_OWNERS       ``<role>:<login>,…`` — the login's words weigh as the owner's for that role only
    CONFIRMERS      ``<role>:<login>[+<login>],…`` — who else may press that role's confirmation
    PLUGINS, PLUGINS_OFF  extensions spec
    SOURCES_OWNER   plugins whose source events weigh as the owner's (default: peer)
    GITLAB_GROUP, MR_CREATE_PATTERNS   the built-in GitLab plugin (gitlab-mr spec)
    EDGE_PUBLIC     where Mattermost reaches the edge for button actions (``host:port`` or URL)
    PRIVATE_GUARD   ``on`` — outgoing text is checked against ``<config>/private-patterns`` (text spec)
    BOT_OWNERS, BOT_SERVICE   agent directory: ``<prefix>:<login>`` owners, service bots listed apart
    PROGRESS, PROGRESS_STEPS, WAIT_NOTIFY_AS   activity (status spec)
    WAITING_POST, WAITING_REMIND_AFTER_H, WAITING_REMIND_REPEAT_H, WAITING_DIGEST_AT   the "Waiting for you" pin
    BACKFILL_DAYS, BACKFILL_SNOOZE_DAYS   questions spec: registry backfill at start

Bot tokens: ``<config>/bots/<connection>/<role>`` (one token per file, 0600). A session is admitted to exactly the
connections that name it; its *own* connection is the default one (``<config>/default``) if that admits it, otherwise the
first that names it in SESSIONS, otherwise the first that admits it by directory. Every event carries ``via=<connection>``,
and the answer goes back to the same connection.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import paths

Connection = Dict[str, str]

# Admission plugins (extensions spec): fn(connection, role) -> True to admit, None to abstain.
ADMISSION: List[Callable[[Connection, str], Optional[bool]]] = []
# Bot plugins: fn(connection, role) -> bot role whose token to use, or None.
BOT_RESOLVERS: List[Callable[[Connection, str], Optional[str]]] = []


def _split(value: str) -> List[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def read_env(path: Path) -> Connection:
    out: Connection = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def connections() -> Dict[str, Connection]:
    out: Dict[str, Connection] = {}
    for p in sorted((paths.config_dir() / "connections").glob("*.env")):
        c = read_env(p)
        c["NAME"] = p.name[:-4]
        out[c["NAME"]] = c
    return out


def default_name() -> Optional[str]:
    try:
        return (paths.config_dir() / "default").read_text().strip() or None
    except FileNotFoundError:
        return None


# ── where a session was started ────────────────────────────────────────────────────────────────────────────────────────
CWD_TTL_S = int(os.environ.get("AIMOST_CWD_TTL_S", "3600"))
_cwd_mem: Dict[str, tuple] = {}


def _claude_root() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path(os.path.expanduser("~")) / ".claude")


def _cwd_cache_file() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(os.environ.get("AIMOST_CWD_CACHE") or Path(base) / f"aimost-session-cwd-{os.getuid()}.json")


def _cwd_disk() -> dict:
    try:
        return json.loads(_cwd_cache_file().read_text())
    except (OSError, ValueError):
        return {}


def _find_transcripts(needle: str) -> List[Path]:
    """Transcripts containing the needle. Transcripts can be hundreds of MB each: never load one into memory. ``grep -lF``
    keeps memory flat; without grep — a streaming scan."""
    root = _claude_root() / "projects"
    if not root.is_dir():
        return []
    try:
        r = subprocess.run(["grep", "-rlF", "--include=*.jsonl", needle, str(root)],
                           capture_output=True, text=True, timeout=60)
        return [Path(x) for x in r.stdout.splitlines()]
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        return []
    hits = []
    for p in root.glob("*/*.jsonl"):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                if any(needle in line for line in f):
                    hits.append(p)
        except OSError:
            pass
    return hits


def file_has(p: Path, needle: str) -> bool:
    """Whether a (possibly huge) file contains the needle — without loading it: ``grep -qF``, else a streaming scan."""
    try:
        return subprocess.run(["grep", "-qF", needle, str(p)], capture_output=True, timeout=60).returncode == 0
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        return False
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return any(needle in line for line in f)
    except OSError:
        return False


TAIL_BYTES = 4 << 20   # how much of a transcript's end ``tail_lines`` reads


def tail_lines(p: Path, max_bytes: Optional[int] = None) -> List[str]:
    """The last lines of a (possibly huge) file, reading at most ``max_bytes`` from its end; the first, cut line is
    dropped when the read did not start at the beginning."""
    max_bytes = max_bytes or TAIL_BYTES
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > max_bytes and lines else lines


def _transcript_cwd(p: Path) -> Optional[str]:
    """The exact cwd recorded in a transcript (every record carries it); fall back to decoding the project directory
    name, which loses dashes (``-home-u-my-proj`` → ``/home/u/my/proj``)."""
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(200), f):
                if '"cwd"' in line:
                    try:
                        cwd = json.loads(line).get("cwd")
                    except ValueError:
                        continue
                    if cwd:
                        return cwd
    except OSError:
        pass
    return "/" + p.parent.name.lstrip("-").replace("-", "/")


def session_cwd(role: str) -> Optional[str]:
    """Start directory of a session: a live one from ``~/.claude/sessions``, a closed one from its newest transcript.
    Closed lookups are cached in memory and on disk (shared by short-lived processes) for ``CWD_TTL_S``."""
    for f in (_claude_root() / "sessions").glob("*.json"):
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if s.get("name") == role and s.get("cwd"):
            return s["cwd"]
    hit = _cwd_mem.get(role) or _cwd_disk().get(role)
    if hit and time.time() - hit[0] < CWD_TTL_S:
        _cwd_mem[role] = tuple(hit)
        return hit[1]
    found = []
    for p in _find_transcripts(f'"customTitle":"{role}"'):
        try:
            found.append((p.stat().st_mtime, p))
        except OSError:
            pass
    cwd = _transcript_cwd(max(found)[1]) if found else None
    _cwd_mem[role] = (time.time(), cwd)
    d = _cwd_disk()
    d[role] = [time.time(), cwd]
    try:
        f = _cwd_cache_file()
        tmp = f.with_name(f"{f.name}.{os.getpid()}")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, f)
    except OSError:
        pass
    return cwd


# ── admission ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def admitted(conn: Connection, role: str, cwd: Optional[str] = None) -> bool:
    if role in _split(conn.get("EXCLUDE", "")):
        return False
    for fn in ADMISSION:
        try:
            if fn(conn, role):
                return True
        except Exception:
            pass
    names = _split(conn.get("SESSIONS", ""))
    if "*" in names or role in names or any("*" in n and fnmatch.fnmatchcase(role, n) for n in names):
        return True
    root = conn.get("SESSIONS_CWD")
    if root:
        cwd = cwd if cwd is not None else session_cwd(role)
        if cwd and (cwd == root or cwd.startswith(root.rstrip("/") + "/")):
            return True
    return False


def own_connection(role: str, cwd: Optional[str] = None) -> Optional[Connection]:
    """The session's own connection. The default connection goes first: alphabetical order would send posts without
    ``--via`` to whichever server sorts first."""
    conns = connections()
    d = default_name()
    if d in conns:
        conns = {d: conns[d], **{k: v for k, v in conns.items() if k != d}}
    for c in conns.values():
        if role in _split(c.get("SESSIONS", "")):
            return c
    for c in conns.values():
        if admitted(c, role, cwd):
            return c
    return None


# ── people with weight ─────────────────────────────────────────────────────────────────────────────────────────────────
def _role_pairs(value: str):
    for x in (value or "").replace(" ", "").split(","):
        role, _, logins = x.partition(":")
        if role and logins:
            yield role, logins


def owners(conn: Connection, role: str) -> List[str]:
    """Logins whose words count as the owner's for this role: OWNER, then the role's CO_OWNERS."""
    own = conn.get("OWNER")
    out = [own] if own else []
    for pat, login in _role_pairs(conn.get("CO_OWNERS", "")):
        if fnmatch.fnmatchcase(role or "", pat) and login not in out:
            out.append(login)
    return out


def confirmers(conn: Connection, role: str) -> List[str]:
    """Logins whose ✅ the role's confirmation accepts: owners and the role's CONFIRMERS (``+`` joins several)."""
    out = owners(conn, role)
    for pat, logins in _role_pairs(conn.get("CONFIRMERS", "")):
        if fnmatch.fnmatchcase(role or "", pat):
            out += [x for x in logins.split("+") if x and x not in out]
    return out


# ── bots ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def bots(conn: Connection) -> Dict[str, str]:
    """Agent roles of a connection: role → bot token."""
    out: Dict[str, str] = {}
    d = paths.config_dir() / "bots" / conn["NAME"]
    for p in sorted(d.glob("*")) if d.is_dir() else []:
        t = p.read_text().strip()
        if t:
            out[p.name] = t
    return out


def bot_role(conn: Connection, role: str) -> Optional[str]:
    """Which bot the session posts with: its own, a shared one by BOT_SHARED, or one named by a bot plugin."""
    own = bots(conn)
    if role in own:
        return role
    for pair in _split(conn.get("BOT_SHARED", "")):
        pat, _, emp = pair.partition(":")
        if pat and emp and fnmatch.fnmatchcase(role, pat) and emp in own:
            return emp
    for fn in BOT_RESOLVERS:
        try:
            emp = fn(conn, role)
        except Exception:
            emp = None
        if emp and emp in own:
            return emp
    return None


def bot(conn: Connection, role: str) -> Optional[str]:
    """Token of the session's bot (see ``bot_role``), or None."""
    emp = bot_role(conn, role)
    return bots(conn)[emp] if emp else None


def has_bot(role: str) -> bool:
    c = own_connection(role)
    return bool(c and bot(c, role))


def relay(conn: Connection, role: str) -> Optional[str]:
    """Relay bot token for an admitted session that has no bot of its own (nor a shared one). ``bot()`` never returns it:
    the relay may post only to the owner's DM and only signed — the post command enforces that."""
    emp = conn.get("RELAY", "").strip()
    if not emp or not role or role == emp or bot(conn, role):
        return None
    tok = bots(conn).get(emp)
    return tok if tok and admitted(conn, role) else None


def thread_route(conn: Connection, bot_name: str, root: str) -> Optional[str]:
    """The session that owns a thread of a shared bot (THREAD_ROUTE), or None (then the bot's own role gets it)."""
    for pair in _split(conn.get("THREAD_ROUTE", "")):
        emp, _, cmd = pair.partition("=")
        if emp == bot_name and cmd and root:
            try:
                r = subprocess.run(cmd.split() + [root], capture_output=True, text=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                return None
            name = r.stdout.strip()
            return name if r.returncode == 0 and name and admitted(conn, name) else None
    return None
