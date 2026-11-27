"""Button posts: a question with buttons and a confirmation ✅/❌ (questions, authority and edge specs).

Rendered by the bridge side when it posts (``lmost post --ask``, ``lmost confirm``) and by the edge when it redraws a post
after a press — one implementation, so both draw the same thing. Every button calls ``http://<public>/action`` of the edge
and carries the signature of the bots' owner (``auth``: ``secret`` and, with per-owner keys, ``kid``).

The question itself (spec) and the answers live in the post's props (``lmost_ask``, ``lmost_answers``), not in the button
context: a context with the full spec is copied into every button and a long question grew past the server's post size.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

Auth = Dict[str, str]


def auth_from(conn: Dict[str, str]) -> Auth:
    """Button signature of a bridge connection: the owner's key and its id (``EDGE_SECRET``, ``EDGE_KEY_ID``)."""
    a = {"secret": conn.get("EDGE_SECRET", "")}
    if conn.get("EDGE_KEY_ID"):
        a["kid"] = conn["EDGE_KEY_ID"]
    return a


def _shown(part: dict, a: dict) -> str:
    return part["options"][a["option"] - 1] if a.get("option") else "“" + a.get("text", "") + "”"


def _mentioned(login: str, said: str) -> bool:
    # on a login boundary, not as a substring: "@ann-bot says" must not hide a mention of @ann; a trailing dot ends a sentence
    return bool(re.search(r"(?<![\w.@-])@" + re.escape(login.lower()) + r"(?![\w-]|\.\w)", said))


def render(q: str, spec: dict, answers: Dict[str, dict], agent: Optional[str], auth: Auth,
           public: str) -> Tuple[str, List[dict]]:
    """Message and attachments of a question post: per part — the choice made, or buttons.

    ``answers``: part number (str) → {option | text, by}. ``q`` — ask id; ids not starting with ``ask-`` belong to an answer
    sink and are shown in the post.
    """
    url = f"http://{public}/action"
    atts: List[dict] = []
    for n, part in enumerate(spec["parts"], 1):
        a = answers.get(str(n))
        att: dict = {"title": f"{n}. {part['title']}", "text": part.get("text", "")}
        if a:
            att["text"] += f"\n✅ **{_shown(part, a)}** — @{a['by']}"
        else:
            ctx = {"q": q, "part": n, **auth, "agent": agent}
            att["actions"] = [{"id": f"p{n}o{m}", "name": ("⭐ " if m == part.get("rec") else "") + opt, "type": "button",
                               "integration": {"url": url, "context": dict(ctx, option=m)}}
                              for m, opt in enumerate(part["options"], 1)]
            att["actions"].append({"id": f"p{n}custom", "name": "✍️ own answer", "type": "button",
                                   "integration": {"url": url, "context": dict(ctx, custom=True)}})
        atts.append(att)
    done = len(answers) >= len(spec["parts"])
    # "discuss in DM": one button per post outside a DM while the question is open — the talk moves to the addressee's DM
    # with the session's bot and does not clutter the shared thread
    if spec.get("discuss") and q.startswith("ask-") and not done:
        ctx = {"kind": "discuss", "q": q, **auth, "to": spec.get("to"), "agent": agent}
        atts.append({"text": "", "actions": [{"id": "discuss", "name": "💬 discuss in DM", "type": "button",
                                              "integration": {"url": url, "context": ctx}}]})
    label = "" if q.startswith("ask-") else f"**{q}** — "
    context = spec.get("context") or ""
    if done:
        # a solved question says "question → answer" by itself, not an empty "solved"
        res = [f"{part['title']} → **{_shown(part, answers[str(n)])}** — @{answers[str(n)]['by']}"
               for n, part in enumerate(spec["parts"], 1)]
        return f"✅ {label}" + (context + "\n" if context else "") + "\n".join(res), atts
    head = f"❓ {label}{context}"
    mention = spec.get("mention") or []
    if mention:
        # addressees are mentioned in the post body with the question itself: a mention inside an attachment does not
        # notify; logins already mentioned in the text are not repeated
        said = (context + " " + " ".join(p.get("title") or "" for p in spec["parts"])).lower()
        tag = [u for u in mention if not _mentioned(u, said)]
        titles = [(lambda t: t if len(t) <= 300 else t[:299] + "…")(" ".join(p["title"].split())) for p in spec["parts"]]
        ask = titles[0] if len(titles) == 1 else "\n".join(f"{n}. {t}" for n, t in enumerate(titles, 1))
        head = (" ".join("@" + u for u in tag) + "\n" if tag else "") + "❓ " + (context + "\n" if context else "") + ask
    head += f"\n_Answered {len(answers)} of {len(spec['parts'])}._"
    return head, atts


def confirm_rule(action: str, details: str) -> str:
    # merging each MR named in the action is let through once per MR by the confirm guard — say so, otherwise the owner
    # does not know how many commands one "yes" covers
    mrs = list(dict.fromkeys(re.findall(r"(?<![\w-])([\w.-]+![0-9]+)\b", f"{action} {details}")))
    if mrs:
        return f"Merging each named MR ({', '.join(mrs)}) — once each; anything else — one “yes”, one action; 15 minutes."
    return "One “yes” — one action, 15 minutes."


def render_confirm(cid: str, action: str, details: str, agent: Optional[str], auth: Auth, public: str,
                   done: Optional[dict] = None, to: Optional[List[str]] = None) -> Tuple[str, List[dict]]:
    """Confirmation post: buttons ✅/❌ or the outcome (``done``: {answer: yes|no, by})."""
    head = f"🛡 **Confirm?** {action}" + (f"\n{details}" if details else "")
    if done:
        return head + f"\n\n{'✅ confirmed' if done['answer'] == 'yes' else '❌ declined'} — @{done['by']}", []
    ctx = {"kind": "confirm", "id": cid, "action": action, "details": details, "agent": agent, **auth}
    if to:
        ctx["to"] = to
    url = f"http://{public}/action"
    return head + f"\n_{confirm_rule(action, details or '')}_", [{"actions": [
        {"id": "yes", "name": "✅ Confirm", "type": "button", "style": "good",
         "integration": {"url": url, "context": dict(ctx, answer="yes")}},
        {"id": "no", "name": "❌ Cancel", "type": "button", "style": "danger",
         "integration": {"url": url, "context": dict(ctx, answer="no")}}]}]
