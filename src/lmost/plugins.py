"""Plugins (extensions spec): discovery, per-connection switching, registration into the core's extension points.

Where plugins come from, in this order:

1. built-ins shipped with l-most (``BUILTIN``) — enabled unless the connection lists them in ``PLUGINS_OFF``;
2. Python entry points, group ``lmost.plugins`` — enabled only where the connection lists them in ``PLUGINS``;
3. files ``<config>/plugins/<name>.py`` — the same as entry points.

A plugin is a module (or an object) with ``NAME``, ``VERSION``, ``KINDS`` (a tuple of extension point names) and
``register(reg)``. ``reg`` is a :class:`Registrar`; what the plugin registers is applied only when ``register`` returns —
a plugin that raises (on import or in ``register``) leaves nothing behind, is shown by ``lmost plugins`` as failed with
the error type, and never stops the caller.

Switching is per connection: a callback that receives a connection (text checks, admission, bot resolvers, answer
sinks) runs only where its plugin is enabled; one that does not (read rules, ``who`` sources, slash commands) is installed
when the plugin is enabled on at least one connection (a built-in — also when there are no connections at all).

Authority (extensions spec): a ``source`` writes events through :meth:`Registrar.inbox`, which sets the sender role
itself — ``peer``, or ``owner`` when the connection lists the plugin in ``SOURCES_OWNER``. A plugin cannot raise it.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import paths
from .connections import Connection, _split

GROUP = "lmost.plugins"
BUILTIN = {"lmost_gitlab": "gitlab"}   # module → plugin name
KINDS = ("transport", "source", "delivery", "command", "answer_sink", "text_check", "admission")

# answer sinks: key → (plugin name, fn(conn, ask record, answer) -> result text; raise to refuse). Read by the bridge.
ANSWER_SINKS: Dict[str, tuple] = {}


class Plugin:
    def __init__(self, name: str, source: str, builtin: bool):
        self.name = name
        self.source = source
        self.builtin = builtin
        self.version = ""
        self.kinds: tuple = ()
        self.state = "loaded"
        self.error = ""

    def row(self) -> Dict[str, str]:
        return {"name": self.name, "kinds": ",".join(self.kinds), "version": self.version, "source": self.source,
                "state": self.state if not self.error else f"failed ({self.error})"}


_plugins: Dict[str, Plugin] = {}
_loaded = False


# ── switching ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def enabled(name: str, conn: Optional[Connection]) -> bool:
    """Is the plugin on for this connection; ``conn`` None (a session with no chat connection) — built-ins only."""
    p = _plugins.get(name)
    builtin = p.builtin if p else name in BUILTIN.values()
    if conn is None:
        return builtin
    if name in _split(conn.get("PLUGINS_OFF", "")):
        return False
    return builtin or name in _split(conn.get("PLUGINS", ""))


def enabled_anywhere(name: str) -> bool:
    from . import connections
    conns = list(connections.connections().values())
    return enabled(name, None) if not conns else any(enabled(name, c) for c in conns)


def source_role(name: str, conn: Optional[Connection]) -> str:
    return "owner" if conn is not None and name in _split(conn.get("SOURCES_OWNER", "")) else "peer"


# ── what a plugin registers ────────────────────────────────────────────────────────────────────────────────────────
class SourceInbox:
    """A role's inbox as a source plugin sees it: ``put`` only, the sender role is the core's."""

    def __init__(self, plugin: str, role: str, conn: Optional[Connection]):
        from . import inbox
        self._box = inbox.Inbox(role)
        self._role = source_role(plugin, conn)
        self._plugin = plugin

    def put(self, text: str, sender: str = "", kind: str = "", **_ignored) -> dict:
        return self._box.put(text, sender=sender or self._plugin, kind=kind or self._plugin, role=self._role)


class Registrar:
    def __init__(self, plugin: Plugin):
        self.plugin = plugin
        self._pending: List[Callable[[], None]] = []

    def _gate(self, fn: Callable, conn_of: Callable) -> Callable:
        name = self.plugin.name

        def gated(*a, **kw):
            return fn(*a, **kw) if enabled(name, conn_of(*a, **kw)) else None
        gated.__name__ = f"{name}.{getattr(fn, '__name__', 'fn')}"
        return gated

    def text_check(self, fn: Callable) -> Callable:
        """fn(draft) — see ``textcheck``: rewrite via ``draft.map_text`` or raise ``textcheck.Refused(20..39, hint)``."""
        from . import textcheck
        g = self._gate(fn, lambda d: d.conn)
        self._pending.append(lambda: textcheck.TEXT_CHECKS.append(g))
        return fn

    def admission(self, fn: Callable) -> Callable:
        """fn(conn, role) -> True (admit) or None (abstain)."""
        from . import connections
        g = self._gate(fn, lambda conn, role: conn)
        self._pending.append(lambda: connections.ADMISSION.append(g))
        return fn

    def bot_resolver(self, fn: Callable) -> Callable:
        """fn(conn, role) -> the bot role whose token to use, or None."""
        from . import connections
        g = self._gate(fn, lambda conn, role: conn)
        self._pending.append(lambda: connections.BOT_RESOLVERS.append(g))
        return fn

    def grant(self, fn: Callable) -> Callable:
        """fn(role, command, hook input) -> reason to count the action as confirmed, or None (``confirm.GRANTS``)."""
        from . import confirm, connections
        g = self._gate(fn, lambda role, cmd, inp: connections.own_connection(role))
        self._pending.append(lambda: confirm.GRANTS.append(g))
        return fn

    def read_rule(self, head: str, fn: Callable) -> Callable:
        """fn(args, ctx) -> None (read-only, passes) or a reason (refused) for a peer turn (``hooks.READ_RULES``)."""
        from . import hooks
        self._pending.append(lambda: enabled_anywhere(self.plugin.name) and hooks.READ_RULES.__setitem__(head, fn))
        return fn

    def who_source(self, fn: Callable) -> Callable:
        """fn(login) -> [{field: value}] for ``lmost who``."""
        from . import cli_who
        self._pending.append(lambda: enabled_anywhere(self.plugin.name) and cli_who.WHO_SOURCES.append(fn))
        return fn

    def command(self, cmd) -> None:
        """A slash command (``commands.Command``)."""
        from . import commands
        self._pending.append(lambda: enabled_anywhere(self.plugin.name) and commands.register(cmd))

    def answer_sink(self, key: str, fn: Callable) -> Callable:
        self._pending.append(lambda: ANSWER_SINKS.__setitem__(key, (self.plugin.name, fn)))
        return fn

    def inbox(self, role: str, conn: Optional[Connection] = None) -> SourceInbox:
        """For a ``source``: the role's inbox with the sender role set by the core."""
        return SourceInbox(self.plugin.name, role, conn)

    def apply(self) -> None:
        for f in self._pending:
            f()


def sink(conn: Connection, key: str) -> Optional[Callable]:
    """The answer sink for a key on this connection, or None (unknown or switched off)."""
    hit = ANSWER_SINKS.get(key)
    return hit[1] if hit and enabled(hit[0], conn) else None


# ── discovery ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def _entry_points() -> List:
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover — Python < 3.8
        return []
    try:
        eps = metadata.entry_points()
        if hasattr(eps, "select"):
            return list(eps.select(group=GROUP))
        return list(eps.get(GROUP, []))  # Python 3.8, 3.9
    except Exception:
        return []


def _local_files() -> List[Path]:
    d = paths.config_dir() / "plugins"
    return sorted(d.glob("*.py")) if d.is_dir() else []


def _import_file(path: Path):
    spec = importlib.util.spec_from_file_location(f"lmost_local_plugin_{path.stem}", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return mod


def _install(p: Plugin, get_module: Callable[[], object]) -> None:
    try:
        mod = get_module()
        p.name = getattr(mod, "NAME", p.name) or p.name
        p.version = str(getattr(mod, "VERSION", ""))
        p.kinds = tuple(getattr(mod, "KINDS", ()))
        bad = [k for k in p.kinds if k not in KINDS]
        if bad:
            raise ValueError(f"unknown kinds {bad}")
        reg = Registrar(p)
        mod.register(reg)
        reg.apply()
    except Exception as e:
        p.state, p.error = "failed", type(e).__name__
        print(f"lmost: plugin {p.name} ({p.source}) not loaded: {type(e).__name__}: {str(e)[:160]}", file=sys.stderr)
    _plugins[p.name] = p


def load(force: bool = False) -> List[Plugin]:
    """Discover and register every plugin once per process."""
    global _loaded
    if _loaded and not force:
        return list(_plugins.values())
    _loaded = True
    for mod, name in BUILTIN.items():
        _install(Plugin(name, "built-in", True), lambda m=mod: importlib.import_module(m))
    for ep in _entry_points():
        dist = getattr(getattr(ep, "dist", None), "name", "") or "?"
        _install(Plugin(ep.name, f"entry point ({dist})", False), ep.load)
    for f in _local_files():
        _install(Plugin(f.stem, str(f), False), lambda f=f: _import_file(f))
    return list(_plugins.values())


def loaded() -> List[Plugin]:
    return list(_plugins.values())
