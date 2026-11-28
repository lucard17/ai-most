"""Built-in optional plugin ``admin`` (admin spec): admin actions in the chat, asked for by a role and executed by the edge
after a confirmer's press — there is no admin secret on the owners' machines.

Off unless enabled: ``AIMOST_PLUGINS=admin`` where the edge runs and where ``aimost commands register`` runs (or
``PLUGINS=admin`` on that connection). The edge then needs ``EDGE_ADMIN_SECRET``, ``EDGE_ADMIN_CONFIRMERS``,
``EDGE_ADMIN_CMD_TOKEN``, ``EDGE_ADMIN_LOG_CHANNEL``, ``EDGE_ADMIN_REQUESTER`` (and optionally ``EDGE_ADMIN_ORIGINS``,
``EDGE_ADMIN_STATE``) — see :mod:`aimost_admin.executor`. The command's name — ``AIMOST_ADMIN_COMMAND`` (``admin``).

    /admin <request post id>           a dry run and a confirmation post (called by the admin role's bot only)
    /admin rollback <result post id>   a rollback, the same way
    aimost admin request --spec <file>|-   ask (on the owner's machine) · aimost admin actions — the list
"""

from __future__ import annotations

NAME = "admin"
VERSION = "0.1.0"
KINDS = ("command",)


def register(reg) -> None:
    from aimost.commands import ROLES, Command
    from . import executor
    reg.command(Command(executor.command_name(), "", "edge", "admin actions: a dry run and a confirmation post",
                        args="<request post id> | rollback <result post id>", who=ROLES, handler=executor.handler))
    reg.edge_route(executor.BUTTON_PATH, executor.route)
