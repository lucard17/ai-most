"""Built-in optional plugin ``health`` (health spec): ``/health`` in the edge — are the services of a domain answering.

Off unless enabled: ``AIMOST_PLUGINS=health`` where the edge runs and where ``aimost commands register`` runs (or
``PLUGINS=health`` in that connection). The edge then reads ``EDGE_HEALTH_TARGETS`` (a JSON file, see
:mod:`aimost_health.config`); ``EDGE_HEALTH=off`` turns the command off without unregistering it.

    /health                    a summary per domain, only the rows that are not green
    /health <domain>           services × contours
    /health <domain> <service> that service per contour, with its whitelisted details

Environment of the edge: ``EDGE_HEALTH_TARGETS``, ``EDGE_HEALTH`` (``off``), ``EDGE_HEALTH_MIN_GAP_S`` (10),
``EDGE_HEALTH_DEADLINE_S`` (5), ``EDGE_HEALTH_PROBE_S`` (1).
"""

from __future__ import annotations

NAME = "health"
VERSION = "0.1.0"
KINDS = ("command",)


def register(reg) -> None:
    from aimost.commands import ROLES, Command
    from .run import handler
    reg.command(Command("health", "", "edge", "are the services of a domain answering", args="[domain [service]]",
                        who=ROLES, handler=handler))
