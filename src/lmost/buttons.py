"""Confirmation post ✅/❌ (authority and edge specs) — drawn by ``lmost confirm`` when it posts and by the edge when it
records the press, one implementation for both. Question posts are drawn by ``lmost.questions.render``.

Buttons call the edge's action URL (``lmost.questions.action_url``) and carry the signature of the bots' owner (``auth`` —
``lmost.questions.client_auth``: ``secret`` and, with per-owner keys, ``kid``).
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

Auth = Dict[str, str]


def confirm_rule(action: str, details: str) -> str:
    # merging each MR named in the action is let through once per MR by the confirm guard — say so, otherwise the owner
    # does not know how many commands one "yes" covers
    mrs = list(dict.fromkeys(re.findall(r"(?<![\w-])([\w.-]+![0-9]+)\b", f"{action} {details}")))
    if mrs:
        return f"Merging each named MR ({', '.join(mrs)}) — once each; anything else — one “yes”, one action; 15 minutes."
    return "One “yes” — one action, 15 minutes."


def render_confirm(cid: str, action: str, details: str, agent: Optional[str], auth: Auth, url: str,
                   done: Optional[dict] = None, to: Optional[List[str]] = None) -> Tuple[str, List[dict]]:
    """Confirmation post: buttons ✅/❌ or the outcome (``done``: {answer: yes|no, by})."""
    head = f"🛡 **Confirm?** {action}" + (f"\n{details}" if details else "")
    if done:
        return head + f"\n\n{'✅ confirmed' if done['answer'] == 'yes' else '❌ declined'} — @{done['by']}", []
    ctx = {"kind": "confirm", "id": cid, "action": action, "details": details, "agent": agent, **auth}
    if to:
        ctx["to"] = to
    return head + f"\n_{confirm_rule(action, details or '')}_", [{"actions": [
        {"id": "yes", "name": "✅ Confirm", "type": "button", "style": "good",
         "integration": {"url": url, "context": dict(ctx, answer="yes")}},
        {"id": "no", "name": "❌ Cancel", "type": "button", "style": "danger",
         "integration": {"url": url, "context": dict(ctx, answer="no")}}]}]
