"""Installing l-most on a machine: config templates and background services (``lmost setup``).

Package data (``lmost/data``): ``config/*.example`` — templates copied into ``<config>`` once (never over the user's own
file); ``systemd/*.service`` — user units, ``launchd/*.plist`` — macOS agents. Placeholders in services:

    @PYTHON@      the interpreter running ``lmost setup``
    @PYTHONPATH@  the directory holding the ``lmost`` package (works for a pip install and for a plain checkout or the
                  Claude Code plugin directory, where nothing is installed)
    @PATH@        directory of ``claude`` (the dispatcher wakes sessions with it), ``~/.local/bin`` and the system dirs
    @LOG@         launchd only: ``<state>/log``

A service file is rewritten only when its content changed, so a repeated ``lmost setup`` touches nothing.
Plugins may add services: ``SERVICES`` holds extra (file name, template text) pairs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import paths

DATA = Path(__file__).resolve().parent / "data"
CORE_UNITS = ("lmost-link.service", "lmost-dispatch.service")
LAUNCHD_PREFIX = "io.github.lucard17."
# Extra services from plugins: (file name, template text) — systemd units or launchd plists by extension.
SERVICES: List[Tuple[str, str]] = []

# template name in data/config → file in <config>
TEMPLATES = {"confirm-patterns.example": "confirm-patterns", "confirm-deny.example": "confirm-deny",
             "private-patterns.example": "private-patterns"}


def unit_dir() -> Path:
    return Path(os.environ.get("LMOST_UNIT_DIR") or paths._xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user")


def launchd_dir() -> Path:
    return Path(os.environ.get("LMOST_LAUNCHD_DIR") or Path(os.path.expanduser("~")) / "Library" / "LaunchAgents")


def systemctl() -> str:
    return os.environ.get("LMOST_SYSTEMCTL", "systemctl")


def values() -> Dict[str, str]:
    claude = shutil.which(os.environ.get("LMOST_CLAUDE_BIN", "claude"))
    path = ([os.path.dirname(claude)] if claude else []) + [
        os.path.join(os.path.expanduser("~"), ".local", "bin"), "/usr/local/bin", "/usr/bin", "/bin"]
    return {"PYTHON": sys.executable, "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
            "PATH": ":".join(dict.fromkeys(path)), "LOG": str(paths.state_dir() / "log")}


def render(text: str, vals: Dict[str, str]) -> str:
    for k, v in vals.items():
        text = text.replace(f"@{k}@", v)
    return text


def write_if_changed(p: Path, text: str) -> bool:
    try:
        if p.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return True


def copy_templates(out: Callable[[str], None] = print) -> List[Path]:
    """Config templates into <config>, only where the user has no file yet (0600)."""
    made = []
    for src, dst in TEMPLATES.items():
        p = paths.config_dir() / dst
        if p.exists():
            continue
        paths.private_dir(p.parent)
        shutil.copyfile(DATA / "config" / src, p)
        os.chmod(p, 0o600)
        out(f"+ {p}" + (" — add your own patterns" if dst == "private-patterns" else ""))
        made.append(p)
    if not list((paths.config_dir() / "connections").glob("*.env")):
        out(f"! no connection: cp {DATA / 'config' / 'connection.env.example'} "
            f"{paths.config_dir() / 'connections'}/<name>.env, fill it in, chmod 600")
    return made


def _services(kind: str) -> List[Tuple[str, str]]:
    ext = ".service" if kind == "systemd" else ".plist"
    own = [(f.name, f.read_text(encoding="utf-8")) for f in sorted((DATA / kind).glob("*"))]
    return own + [(n, t) for n, t in SERVICES if n.endswith(ext) or (kind == "systemd" and n.endswith(".timer"))]


def install_systemd(start: bool, out: Callable[[str], None] = print) -> List[str]:
    """Render the user units; reload systemd; enable and (re)start the core ones when ``start``. Returns changed names."""
    vals, changed = values(), []
    for name, text in _services("systemd"):
        if write_if_changed(unit_dir() / name, render(text, vals)):
            out(f"+ {unit_dir() / name}")
            changed.append(name)
    run = lambda *a: subprocess.run([systemctl(), "--user", *a], capture_output=True, text=True)  # noqa: E731
    run("daemon-reload")
    if start:
        run("enable", "-q", *CORE_UNITS)
        # restart only what changed or is not running: a repeated setup does not cut a working bridge
        for u in CORE_UNITS:
            if u in changed or run("is-active", "-q", u).returncode != 0:
                run("restart", u)
        for name in changed:
            if name.endswith(".timer"):
                run("enable", "--now", "-q", name)
    else:
        out("! no bots yet — units installed but not started: put a bot token into "
            f"{paths.config_dir() / 'bots'}/<connection>/<role> (or `lmost bot add`), then run `lmost setup` again")
    return changed


def install_launchd(out: Callable[[str], None] = print) -> List[str]:
    """Render the launchd agents (macOS). Loading them is left to the user — the command is printed."""
    vals, changed = values(), []
    paths.private_dir(Path(vals["LOG"]))
    for name, text in _services("launchd"):
        p = launchd_dir() / name
        if write_if_changed(p, render(text, vals)):
            out(f"+ {p}")
            changed.append(name)
    if changed:
        out("load them: " + "; ".join(f"launchctl bootstrap gui/$(id -u) {launchd_dir() / n}" for n in changed))
    return changed


def has_bots() -> bool:
    d = paths.config_dir() / "bots"
    return d.is_dir() and any(p.is_file() for p in d.glob("*/*"))


def units_check(out: Callable[[str], None] = print) -> int:
    bad = 0
    for u in CORE_UNITS:
        r = subprocess.run([systemctl(), "--user", "is-active", "-q", u], capture_output=True)
        if r.returncode == 0:
            out(f"  ✓ {u} active")
        else:
            out(f"  ✗ {u} not active ({systemctl()} --user status {u})")
            bad = 1
    return bad


def service_manager(no_services: bool = False) -> Optional[str]:
    """systemd | launchd | None (asked not to, or neither is there)."""
    if no_services:
        return None
    forced = os.environ.get("LMOST_SERVICE_MANAGER")
    if forced:
        return None if forced == "none" else forced
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux") and shutil.which(systemctl()):
        return "systemd"
    return None
