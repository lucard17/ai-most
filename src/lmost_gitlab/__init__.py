"""Built-in GitLab plugin (gitlab-mr spec).

* ``source`` — merge requests a session created are linked to it (PostToolUse hook ``lmost gitlab hook``), and their
  activity (approvals, people's notes, a failed pipeline, merged/closed) arrives in the session's inbox
  (``lmost gitlab once`` from a timer);
* ``text_check`` — ``repo!N`` in outgoing text becomes a link with the MR title; a bare ``!N`` is refused (code 20);
* a ``who`` directory — GitLab users for ``lmost who``.

Everything goes through the ``glab`` CLI (``LMOST_GLAB_BIN``) with the account of whoever runs it. Without ``glab``
the plugin stays silent. Projects: ``repo!N`` is resolved in the connection's ``GITLAB_GROUP`` (``LMOST_GITLAB_GROUP``).
"""

from __future__ import annotations

NAME = "gitlab"
VERSION = "0.1.0"
KINDS = ("source", "text_check")


def register(reg) -> None:
    from . import mrlinks, who
    reg.text_check(mrlinks.check)
    reg.who_source(who.search)
