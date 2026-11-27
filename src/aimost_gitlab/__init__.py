"""Built-in GitLab plugin (gitlab-mr spec).

* ``source`` — merge requests a session created are linked to it (PostToolUse hook ``aimost gitlab hook``), and their
  activity (approvals, people's notes, a failed pipeline, merged/closed) arrives in the session's inbox
  (``aimost gitlab once`` from a timer);
* ``text_check`` — ``repo!N`` in outgoing text becomes a link with the MR title; a bare ``!N`` is refused (code 20);
* a ``who`` directory — GitLab users for ``aimost who``;
* merge request cards in confirmation posts (title, state, CI on the head commit).

Everything goes through the ``glab`` CLI (``AIMOST_GLAB_BIN``) with the account of whoever runs it. Without ``glab``
the plugin stays silent. Projects: ``repo!N`` is resolved in the connection's ``GITLAB_GROUP`` (``AIMOST_GITLAB_GROUP``).
"""

from __future__ import annotations

NAME = "gitlab"
VERSION = "0.1.0"
KINDS = ("source", "text_check")


SERVICE = """[Unit]
Description=aimost gitlab — activity of linked merge requests into the sessions' inboxes

[Service]
Type=oneshot
ExecStart=@PYTHON@ -m aimost gitlab once
Environment=PATH=@PATH@
Environment=PYTHONPATH=@PYTHONPATH@
"""

TIMER = """[Unit]
Description=aimost gitlab — poll linked merge requests

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
"""


def register(reg) -> None:
    from . import cards, glab, mrlinks, who
    reg.text_check(mrlinks.check)
    reg.confirm_card(cards.card)
    reg.who_source(who.search)
    if glab.available():                 # no glab — no timer either
        reg.service("aimost-gitlab.service", SERVICE)
        reg.service("aimost-gitlab.timer", TIMER)
