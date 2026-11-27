"""Read receipts (status spec): 👀 by the bot that delivered a post, once the session has read it (inbox ack).

Kept apart from ``link`` so that every inbox reader (hooks, ``lmost inbox``) registers it without importing the bridge.
"""

from __future__ import annotations

from typing import List

from . import activity


def on_ack(role: str, events: List[dict]) -> None:
    """👀 on the posts the session has just read — in the background; a failing receipt never breaks reading."""
    for e in events:
        if e.get("via") and e.get("post"):
            activity.react(e["via"], role, e["post"], "eyes")
