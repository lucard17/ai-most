"""Question upkeep on the bridge side (questions spec): answers from the edge's marks, the "Waiting for you" pin, reminders,
snoozes, "not relevant", discussions in a DM, deadlines and the registry backfill.

The bridge (``aimost link``) calls two things:

* ``on_post(conn, post)`` — for every new version of a post written by one of the connection's bots: the edge leaves
  its marks in the post's props (``aimost_answers``, ``aimost_waiting_drop``, ``aimost_waiting_snooze``, ``aimost_discuss``)
  and this module acts on them once. Marks on posts of other authors are ignored: props are written by the post's author,
  and a colleague's agent could otherwise forge "the owner answered".
* ``tick(conn)`` — every loop: deadlines (``--ttl``), the pin, reminders. Cheap when nothing changed.

``backfill(conn, days)`` records into the registry open button questions asked past it (``aimost link backfill-asks``).

Connection keys: ``WAITING_POST`` — the bot role that keeps the pin and writes reminders (off when empty),
``WAITING_REMIND_AFTER_H`` (1), ``WAITING_REMIND_REPEAT_H`` (2), ``WAITING_DIGEST_AT`` (``10:00,18:00``, local time),
``BACKFILL_DAYS`` (14), ``BACKFILL_SNOOZE_DAYS`` (7).

Answer sinks (extensions spec): registered by plugins in ``plugins.ANSWER_SINKS`` (interface there). Only the owner's
press reaches a sink; a sink switched off for the connection counts as not installed.

State — ``paths.link_state_dir()``: ``<conn>.answered.json``, ``<conn>.waiting.json``, ``<conn>.remind.json``; snoozes and
discussions — ``askstore``.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import askstore, connections, inbox, mattermost, paths, plugins, questions
from .linkctx import Ctx, _ctx, context  # noqa: F401  (the per-connection context lives with the bridge)
from .names import P

MARK_PROPS = (P("answers"), P("waiting_drop"), P("waiting_snooze"), P("discuss"))
PIN_PROP = P("waiting")
CHAT_SECTION = "Chat questions"
READBACK = (f"\n{inbox.FREE_TEXT_MARK} — before acting, retell it in the thread by its consequences: what changes, what "
            "does not, an example; with buttons “✅ right” / “✏️ not so” (`aimost post --thread <thread> --to <who> "
            "--ask …`)")
SNOOZE_LOCK = threading.Lock()      # <conn>.snooze.json: the main loop and the backfill thread


SinkRefused = plugins.SinkRefused


def log(msg: str) -> None:
    import sys
    sys.stderr.write("aimost link: " + msg + "\n")
    sys.stderr.flush()


def _f(conn: dict, key: str, default: float) -> float:
    try:
        return float(conn.get(key) or default)
    except ValueError:
        log(f"[{conn.get('NAME')}] {key}={conn.get(key)!r} is not a number, using {default:g}")
        return float(default)


def _jload(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _jsave(p: Path, obj) -> None:
    paths.private_dir(p.parent)
    tmp = p.with_name("." + p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False))
    os.replace(tmp, p)


def _now_ms() -> int:
    return int(time.time() * 1000)


def age(ms: float, now_ms: float) -> str:
    m = max(0, int((now_ms - ms) / 60000))
    return f"{m} min" if m < 60 else f"{m // 60} h" if m < 48 * 60 else f"{m // 1440} d"


# ── per-connection context ─────────────────────────────────────────────────────────────────────────────────────────────
def put_event(to_role: str, text: str, **kw) -> None:
    try:
        inbox.Inbox(to_role).put(text, **kw)
    except Exception as e:
        log(f"{to_role}: event not written: {e}")


def cancel_in_place(ctx: Ctx, token: str, post: dict, why: str, by: str) -> int:
    patch = questions.cancelled_patch(post, why, by=by)
    mattermost.api(ctx.c, "PUT", f"/posts/{post['id']}/patch", patch, token=token)
    return patch["props"][P("cancelled")]["at"]


# ── marks of the edge on a post ────────────────────────────────────────────────────────────────────────────────────────
def on_post(conn: dict, post: dict, who: Optional[str] = None) -> None:
    """Act on the edge's marks in a post's props. Only posts written by the connection's own bots count."""
    props = post.get("props") or {}
    if not any(props.get(k) for k in MARK_PROPS):
        return
    ctx = context(conn)
    if not ctx.bots or post.get("user_id") not in ctx.ids.values():
        return
    who = who or next((n for n, i in ctx.ids.items() if i == post.get("user_id")), None)
    for key, fn in ((P("answers"), on_answers), (P("waiting_drop"), on_waiting_drop),
                    (P("waiting_snooze"), on_waiting_snooze), (P("discuss"), on_discuss)):
        if props.get(key):
            try:
                fn(ctx, post, who)
            except Exception as e:
                log(f"[{ctx.name}] post {post.get('id', '')[:6]}: {key}: {e}")


def _shown(parts: list, part: int, a: dict) -> str:
    if a.get("text"):
        return a["text"]
    if 0 < part <= len(parts):
        opts = parts[part - 1].get("options") or []
        if 0 < int(a.get("option") or 0) <= len(opts):
            return opts[a["option"] - 1]
    return f"option {a.get('option')}"


def on_answers(ctx: Ctx, p: dict, who: Optional[str]) -> None:
    """Answers recorded by the edge (``aimost_answers`` {part: {option|text, by, user_id, at}}): once each, only from
    those allowed — the addressees and the asking role's owners; a sink question — only the owner. An answer given after
    the question was cancelled does not count. The answer goes to the asking session as an ``answer`` event."""
    props = p.get("props") or {}
    q = str(props.get(P("question")) or "")
    asker = props.get("agent")
    spec = props.get(P("ask")) or {}
    if not q.startswith("ask-") or not asker:
        return
    sink = spec.get("sink") or ""
    allowed = {ctx.owner} if sink else (ctx.owner_ids(asker) | ctx.ids_of(spec.get("to")))
    rec = askstore.asks(asker).get(q)
    gone = [((props.get(P("cancelled")) or {}).get("at")), (rec or {}).get("cancelled_at")]
    gone = min([x for x in gone if x], default=None)
    ap = ctx.state("answered")
    answered = set(_jload(ap, []))
    parts = spec.get("parts") or []
    for part, a in sorted((props.get(P("answers")) or {}).items()):
        k = f"{p['id']}:{part}:{a.get('at')}"
        if k in answered or a.get("user_id") not in allowed:
            continue
        answered.add(k)
        _jsave(ap, sorted(answered)[-5000:])
        if gone and (a.get("at") or 0) >= gone:
            log(f"[{ctx.name}] {q} item {part}: the question was cancelled — the answer does not count")
            continue
        n = int(part)
        title = parts[n - 1].get("title", "") if 0 < n <= len(parts) else ""
        shown = _shown(parts, n, a)
        by = ctx.user(a["user_id"])["username"]
        thread = p.get("root_id") or p["id"]
        if sink:
            text = _to_sink(ctx, sink, q, n, a, title, p, thread, who)
            if text is None:
                continue
            role = "owner"
        else:
            text = f"answer to {q} item {part} «{title}»: {shown}" + (READBACK if a.get("text") else "")
            role = "owner" if a["user_id"] in ctx.owner_ids(asker) else "person"
        if connections.admitted(ctx.c, asker):
            put_event(asker, text, sender=by, kind="answer", via=ctx.name, role=role, thread=thread)
        log(f"[{ctx.name}] {q} item {part} → {asker}")
    if rec and rec.get("post") == p["id"] and rec.get("state", "open") == "open" and not gone \
            and askstore.ask_state(p) == "answered":
        askstore.ask_record(asker, {"q": q, "state": "answered"})


def _to_sink(ctx: Ctx, sink: str, q: str, part: int, a: dict, title: str, p: dict, thread: str,
             who: Optional[str]) -> Optional[str]:
    s = plugins.sink(ctx.c, sink)
    try:
        if s is None:
            raise SinkRefused("not installed")
        result = s.answer(ctx.c, q, part, a, title)
    except Exception as e:
        msg = str(e)[:300] or type(e).__name__
        log(f"[{ctx.name}] {q}: {sink} refused: {msg}")
        try:
            ctx.api("POST", "/posts", {"channel_id": p["channel_id"], "root_id": thread,
                                       "message": f"⚠️ {sink} refused: {msg}"}, who=who)
        except Exception as x:
            log(f"[{ctx.name}] {q}: refusal not posted: {x}")
        return None
    return f"answer to {q} item {part} «{title}» → {sink}: {result}"


def on_waiting_drop(ctx: Ctx, p: dict, who: Optional[str]) -> None:
    """"not relevant" in the pin (``aimost_waiting_drop`` {ask id: {asker, user_id, at}}): the owner's press cancels the
    question in place with the asking session's bot, the registry says cancelled, the session gets ``cancelled``."""
    props = p.get("props") or {}
    if not props.get(PIN_PROP):
        return
    for q, m in sorted((props.get(P("waiting_drop")) or {}).items()):
        asker = m.get("asker") or ""
        if m.get("user_id") != ctx.owner or not asker:
            continue
        rec = askstore.asks(asker).get(q)
        if not rec or rec.get("state", "open") != "open" or rec.get("via") != ctx.name:
            continue
        tok = connections.bot(ctx.c, asker)
        if not tok:
            continue
        why = f"dropped by @{ctx.c.get('OWNER')} from Waiting for you"
        try:
            post = mattermost.api(ctx.c, "GET", f"/posts/{rec['post']}", token=tok)
            at = cancel_in_place(ctx, tok, post, why, by=ctx.c.get("OWNER", ""))
        except Exception as e:
            log(f"[{ctx.name}] {q}: not relevant — not cancelled ({e})")
            continue
        askstore.ask_record(asker, {"q": q, "state": "cancelled", "cancelled_at": at, "why": why})
        put_event(asker, f"question {q} «{rec.get('title', '')}» cancelled by the owner from Waiting for you — not "
                         "relevant", sender=ctx.c.get("OWNER", ""), kind="cancelled", via=ctx.name, role="owner",
                  thread=rec.get("thread") or rec["post"])
        log(f"[{ctx.name}] {q} ({asker}): {why}")


def on_waiting_snooze(ctx: Ctx, p: dict, who: Optional[str]) -> None:
    """"snooze…" / "bring back…" in the pin (``aimost_waiting_snooze`` {key: {value, asker, user_id, at}}; value — 1d |
    7d | forever | back:<key>): the owner's press goes into the snooze file once (its ``at`` is remembered), the asking
    session gets ``snoozed``."""
    props = p.get("props") or {}
    marks = props.get(P("waiting_snooze")) or {}
    if not props.get(PIN_PROP) or not marks:
        return
    with SNOOZE_LOCK:
        _snooze_marks(ctx, marks)


def _snooze_marks(ctx: Ctx, marks: dict) -> None:
    path = askstore.snooze_path(ctx.name)
    d = _jload(path, {})
    items, done = d.setdefault("items", {}), d.setdefault("marks", {})
    changed = False
    for k, m in sorted(marks.items()):
        v, at = str(m.get("value") or ""), m.get("at") or 0
        if v.startswith("back:"):
            k, v = v[5:], "back"
        if m.get("user_id") != ctx.owner or not k or done.get(k, 0) >= at:
            continue
        done[k], changed = at, True
        prev = items.get(k) or {}
        if v == "back":
            if items.pop(k, None) is None:
                continue
            text = "brought back into Waiting for you"
        elif v in ("1d", "7d", "forever"):
            until = None if v == "forever" else at + (1 if v == "1d" else 7) * 86400 * 1000
            items[k] = {"until": until, "by": ctx.c.get("OWNER"), "at": at, "asker": m.get("asker") or ""}
            text = "snoozed in Waiting for you " + ("until they say" if until is None else
                                                    "until " + time.strftime("%d.%m %H:%M", time.localtime(until / 1000)))
        else:
            continue
        asker = m.get("asker") or prev.get("asker") or ""
        rec = askstore.asks(asker).get(k) if asker else None
        if rec:
            put_event(asker, f"@{ctx.c.get('OWNER')} {text} question {k} «{rec.get('title', '')}»"
                             + (" — it is out of the pin and reminders while snoozed" if v != "back" else ""),
                      sender=ctx.c.get("OWNER", ""), kind="snoozed", via=ctx.name, role="owner",
                      thread=rec.get("thread") or rec.get("post") or "")
        log(f"[{ctx.name}] {k}: {text}")
    if changed:
        now = _now_ms()
        d["items"] = {k: v for k, v in items.items() if v.get("until") is None or v["until"] > now}
        d["marks"] = dict(sorted(done.items(), key=lambda x: x[1])[-500:])
        _jsave(path, d)


def on_discuss(ctx: Ctx, p: dict, who: Optional[str]) -> None:
    """"💬 discuss in DM" (``aimost_discuss`` {user_id: {at, by}}): for an addressee or an owner of the asking role — a
    thread "Discussion: …" in their DM with the session's bot, a line in the original thread, a ``discuss`` event. One
    thread per person and post."""
    props = p.get("props") or {}
    q, asker, spec = str(props.get(P("question")) or ""), props.get("agent"), props.get(P("ask")) or {}
    if not q.startswith("ask-") or not asker or not connections.admitted(ctx.c, asker):
        return
    tok = connections.bot(ctx.c, asker)
    if not tok:
        return
    allowed = ctx.owner_ids(asker) | ctx.ids_of(spec.get("to"))
    state = askstore.discussions(ctx.name)
    done = {(v.get("post"), v.get("user_id")) for v in state.values()}

    def api(method, path, body=None):
        return mattermost.api(ctx.c, method, path, body, token=tok)

    for uid in sorted(props.get(P("discuss")) or {}):
        if uid not in allowed or (p["id"], uid) in done:
            continue
        user = ctx.user(uid)["username"]
        title = " ".join(questions.title(spec).split()) or q
        dmc = api("POST", "/channels/direct", [api("GET", "/users/me")["id"], uid])["id"]
        root = api("POST", "/posts", {"channel_id": dmc, "props": {"agent": asker},
                                      "message": f"💬 Discussion: {title} — [original post]({ctx.link(p['id'])})"})["id"]
        thread = p.get("root_id") or p["id"]
        state[root] = {"asker": asker, "q": q, "post": p["id"], "thread": thread, "channel": p["channel_id"],
                       "user": user, "user_id": uid, "dm_channel": dmc, "at": _now_ms()}
        _jsave(askstore.discussions_path(ctx.name), state)
        api("POST", "/posts", {"channel_id": p["channel_id"], "root_id": thread, "props": {"agent": asker},
                               "message": f"💬 discussion moved to DM with @{user}"})
        put_event(asker, f"discussion of question {q} «{title}» — @{user} opened it in a DM: talk in this thread; the "
                         f"outcome — `aimost post --via {ctx.name} --conclude {root} \"<outcome>\"` (one line into the "
                         "original thread)",
                  sender=user, kind="discuss", via=ctx.name, channel="dm", thread=root, post=root,
                  role="owner" if uid in ctx.owner_ids(asker) else "person")
        log(f"[{ctx.name}] {q}: discussion in DM with @{user} → {asker}")


# ── deadlines ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def expire_asks(ctx: Ctx, now_ms: Optional[float] = None) -> None:
    """``--ttl <hours>``: unanswered by the deadline — cancelled with "deadline N h passed". Only this connection's
    questions; the server is asked only about the expired ones."""
    now = _now_ms() if now_ms is None else now_ms
    for who in askstore.ask_roles():
        for q, r in askstore.asks(who).items():
            if r.get("state", "open") != "open" or r.get("via") != ctx.name or not r.get("ttl") \
                    or now < (r.get("at") or 0) + float(r["ttl"]) * 3600 * 1000:
                continue
            tok = connections.bot(ctx.c, who)
            if not tok:
                continue
            try:
                post = mattermost.api(ctx.c, "GET", f"/posts/{r['post']}", token=tok)
                st = "gone" if post.get("delete_at") else askstore.ask_state(post)
                if st == "open":
                    why = f"deadline {float(r['ttl']):g} h passed"
                    at = cancel_in_place(ctx, tok, post, why, by="aimost link")
                    askstore.ask_record(who, {"q": q, "state": "cancelled", "cancelled_at": at, "why": why})
                    log(f"[{ctx.name}] {q} ({who}): {why} — cancelled")
                else:
                    askstore.ask_record(who, {"q": q, "state": st})
            except urllib.error.HTTPError as e:
                if e.code in (403, 404):
                    askstore.ask_record(who, {"q": q, "state": "gone"})
                else:
                    log(f"[{ctx.name}] {q}: deadline — not cancelled ({e})")
            except Exception as e:
                log(f"[{ctx.name}] {q}: deadline — not cancelled ({e})")


# ── what waits for the owner ───────────────────────────────────────────────────────────────────────────────────────────
def _sink_rows(ctx: Ctx) -> List[dict]:
    rows = []
    for key, s in sorted(plugins.sinks(ctx.c).items()):
        fn: Optional[Callable] = getattr(s, "rows", None)
        if not fn:
            continue
        try:
            for r in fn(ctx.c) or []:
                rows.append({"blocking": bool(r.get("blocking")), "at": r.get("at") or _now_ms(), "who": key,
                             "title": " ".join(str(r.get("title") or r.get("key")).split())[:100],
                             "link": r.get("link") or "", "key": f"{key}:{r.get('key')}", "section": key})
        except Exception as e:
            log(f"[{ctx.name}] {key}: open questions not read: {e}")
    return rows


def waiting_rows(ctx: Ctx, snoozed: bool = False, now_ms: Optional[float] = None) -> List[dict]:
    """Open questions to the owner from every session of this connection (the registry) and from answer sinks; 🔴 on
    top, then the longest waiting. Snoozed ones are not here (``snoozed=True`` — only them, with ``until``)."""
    owner, now = ctx.c.get("OWNER"), _now_ms() if now_ms is None else now_ms
    rows = []
    for who in askstore.ask_roles():
        for q, r in askstore.asks(who).items():
            if r.get("state", "open") != "open" or r.get("via") != ctx.name or owner not in (r.get("to") or []):
                continue
            rows.append({"blocking": bool(r.get("blocking")), "at": r.get("at") or now, "who": who,
                         "title": r.get("title") or q, "link": ctx.link(r.get("post")), "q": q, "asker": who,
                         "key": q, "section": r.get("sink") or CHAT_SECTION})
    rows += _sink_rows(ctx)
    sn = askstore.snoozes(ctx.name, now_ms=now)
    if snoozed:
        rows = [dict(r, until=sn[r["key"]].get("until")) for r in rows if r["key"] in sn]
    else:
        rows = [r for r in rows if r["key"] not in sn]
    rows.sort(key=lambda x: (not x["blocking"], x["at"]))
    return rows


def render_pin(ctx: Ctx, rows: List[dict], later: List[dict], now_ms: float):
    """(message, attachments) of the "Waiting for you" pin: sections, per line "not relevant" (chat questions only — a
    sink's question is dropped in the sink) and "snooze…"; "💤 snoozed: N" with "bring back…". Action ids are unique per
    line: Mattermost finds the pressed action by id and takes the first match."""
    url, own = questions.action_url(ctx.c), [ctx.c.get("OWNER")]
    auth = questions.client_auth(ctx.c)

    def menu(r, name, options, i):
        return {"id": f"snooze{i}", "name": name, "type": "select",
                "options": [{"text": t, "value": v} for t, v in options],
                "integration": {"url": url, "context": {"kind": "waiting-snooze", "key": r["key"],
                                                        "asker": r.get("asker") or "", "to": own, **auth}}}

    atts = []
    for i, r in enumerate(rows):
        acts = []
        if r.get("q"):
            acts.append({"id": f"drop{i}", "name": "not relevant", "type": "button", "integration": {
                "url": url, "context": {"kind": "waiting-drop", "q": r["q"], "asker": r["asker"], "to": own, **auth}}})
        acts.append(menu(r, "snooze…", [("a day", "1d"), ("a week", "7d"), ("until I say", "forever")], i))
        atts.append({"text": ("🔴 " if r["blocking"] else "") + f"{r['who']} · {r['title'][:100]}", "actions": acts})
    atts = atts[:19]
    if later:
        atts.append({"text": f"💤 snoozed: {len(later)}", "actions": [menu(
            {"key": ""}, "bring back…", [(f"{r['who']} · {r['title'][:60]}", "back:" + r["key"]) for r in later[:100]],
            "back")]})

    def line(r):
        return "- " + ("🔴 " if r["blocking"] else "") + f"{r['who']} · {r['title'][:100]} · waiting {age(r['at'], now_ms)}" \
            + (f" · [post]({r['link']})" if r["link"] else "")

    sections: Dict[str, List[dict]] = {}
    for r in rows:
        sections.setdefault(r["section"], []).append(r)
    order = ([CHAT_SECTION] if CHAT_SECTION in sections else []) + sorted(k for k in sections if k != CHAT_SECTION)
    parts = [f"**{k} — {len(sections[k])}**\n" + "\n".join(map(line, sections[k])) for k in order]
    msg = (f"📋 Waiting for you — {len(rows)}\n" + "\n".join(parts) if rows else "📋 Waiting for you — nothing ✅") \
        + (f"\n💤 snoozed: {len(later)}" if later else "") \
        + "\n_Updates itself. Answer in the question's post; 🔴 — work stops without the answer; “snooze…” — out of the " \
          "list for a while._"
    return msg, atts


def waiting_update(ctx: Ctx, now: Optional[float] = None, _marks_done: bool = False) -> None:
    """One pinned post of the ``WAITING_POST`` bot in the owner's DM, edited in place when its contents change (at most
    every ``AIMOST_WAITING_EVERY_S``, 30 s); the ages are refreshed hourly. The pin's own marks are applied first: a
    redraw replaces props whole and would wipe an unprocessed press."""
    emp = ctx.c.get("WAITING_POST")
    if not emp or emp not in ctx.bots or not ctx.owner:
        return
    sp = ctx.state("waiting")
    st = _jload(sp, {})
    now = time.time() if now is None else now
    rows, later = waiting_rows(ctx, now_ms=now * 1000), waiting_rows(ctx, snoozed=True, now_ms=now * 1000)
    key = json.dumps([[r["who"], r["title"], r["blocking"], r["link"]] for r in rows] + [r["key"] for r in later],
                     ensure_ascii=False)
    if st.get("post") and st.get("key") == key and now - st.get("at", 0) < 3600:
        return
    if now - st.get("at", 0) < float(os.environ.get("AIMOST_WAITING_EVERY_S", "30")):
        return
    if st.get("post") and not _marks_done:
        try:
            pin = ctx.api("GET", f"/posts/{st['post']}", who=emp)
            pp = pin.get("props") or {}
            if pp.get(P("waiting_drop")) or pp.get(P("waiting_snooze")):
                on_waiting_drop(ctx, pin, emp)
                on_waiting_snooze(ctx, pin, emp)
                return waiting_update(ctx, now, _marks_done=True)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log(f"[{ctx.name}] Waiting for you: pin marks not read, redraw next pass: {e}")
                return
        except Exception as e:
            log(f"[{ctx.name}] Waiting for you: pin marks not read, redraw next pass: {e}")
            return
    msg, atts = render_pin(ctx, rows, later, now * 1000)
    props = {"agent": emp, PIN_PROP: 1, "attachments": atts}
    try:
        if st.get("post"):
            try:
                ctx.api("PUT", f"/posts/{st['post']}/patch", {"message": msg, "props": props}, who=emp)
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                st.pop("post", None)
        if not st.get("post"):
            ch = ctx.api("POST", "/channels/direct", [ctx.ids[emp], ctx.owner], who=emp)["id"]
            pid = ctx.api("POST", "/posts", {"channel_id": ch, "message": msg, "props": props}, who=emp)["id"]
            ctx.api("POST", f"/posts/{pid}/pin", who=emp)
            st["post"] = pid
        st.update(key=key, at=now)
        _jsave(sp, st)
    except Exception as e:
        log(f"[{ctx.name}] Waiting for you not updated: {e}")


def waiting_remind(ctx: Ctx, now: Optional[float] = None) -> None:
    """Reminders in the owner's DM by the ``WAITING_POST`` bot: a 🔴 question waiting longer than
    ``WAITING_REMIND_AFTER_H`` — one post with a link, repeated at most every ``WAITING_REMIND_REPEAT_H``; at
    ``WAITING_DIGEST_AT`` (within an hour of the slot) — a digest and the three oldest; nothing waiting — no digest."""
    emp = ctx.c.get("WAITING_POST")
    if not emp or emp not in ctx.bots or not ctx.owner:
        return
    after = _f(ctx.c, "WAITING_REMIND_AFTER_H", 1) * 3600
    every = _f(ctx.c, "WAITING_REMIND_REPEAT_H", 2) * 3600
    now = time.time() if now is None else now
    lt = time.localtime(now)
    sp = ctx.state("remind")
    st = _jload(sp, {})
    st0 = json.dumps(st, sort_keys=True)
    sent, digests = st.setdefault("asks", {}), st.setdefault("digests", [])
    rows = waiting_rows(ctx, now_ms=now * 1000)

    def line(r):
        return ("🔴 " if r["blocking"] else "") + f"{r['who']} · «{r['title'][:100]}» · waiting {age(r['at'], now * 1000)}" \
            + (f" · [post]({r['link']})" if r.get("link") else "")

    out = []
    for r in rows:
        if not r.get("blocking") or not r.get("q") or now * 1000 - r["at"] < after * 1000:
            continue
        if now - sent.get(r["q"], 0) < every:
            continue
        sent[r["q"]] = now
        out.append((f"⏰ Waiting for an answer, work stops without it: {line(r)}", {P("remind"): r["q"]}))
    live = {r.get("q") for r in rows}
    st["asks"] = {q: t for q, t in sent.items() if q in live}
    for slot in [x.strip() for x in (ctx.c.get("WAITING_DIGEST_AT", "10:00,18:00") or "").split(",") if x.strip()]:
        try:
            h, m = (int(x) for x in slot.split(":"))
        except ValueError:
            log(f"[{ctx.name}] WAITING_DIGEST_AT: {slot!r} is not hh:mm")
            continue
        start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
        key = time.strftime("%Y-%m-%d ", lt) + slot
        if not (start <= now < start + 3600) or key in digests:
            continue
        digests.append(key)
        del digests[:-10]
        if not rows:
            continue
        old = sorted(rows, key=lambda x: x["at"])[:3]
        out.append((f"📋 Digest: {len(rows)} waiting, {sum(1 for r in rows if r['blocking'])} of them 🔴. The oldest:\n"
                    + "\n".join(f"{i}. {line(r)}" for i, r in enumerate(old, 1))
                    + "\n_The whole list — in the pinned “Waiting for you”._", {P("digest"): key}))
    try:
        if out:
            ch = ctx.api("POST", "/channels/direct", [ctx.ids[emp], ctx.owner], who=emp)["id"]
            for msg, props in out:
                ctx.api("POST", "/posts", {"channel_id": ch, "message": msg, "props": {"agent": emp, **props}}, who=emp)
    except Exception as e:
        log(f"[{ctx.name}] reminder not sent: {e}")
        return                                   # state not written — retried next pass
    if json.dumps(st, sort_keys=True) != st0:
        _jsave(sp, st)


def tick(conn: dict, now: Optional[float] = None) -> None:
    """One bridge loop pass for a connection: deadlines, the pin, reminders. Never raises."""
    try:
        ctx = context(conn)
        if not ctx.bots:
            return
        expire_asks(ctx, None if now is None else now * 1000)
        waiting_update(ctx, now)
        waiting_remind(ctx, now)
    except Exception as e:
        log(f"[{conn.get('NAME')}] waiting: {e}")


# ── registry backfill ──────────────────────────────────────────────────────────────────────────────────────────────────
def backfill(conn: dict, days: Optional[float] = None) -> int:
    """Open button questions of the connection's bots within ``days`` (``BACKFILL_DAYS``, 14) missing from the
    registry — into it; older than ``BACKFILL_SNOOZE_DAYS`` (7) — snoozed "until I say" right away. Returns the count."""
    ctx = context(conn)
    days = _f(conn, "BACKFILL_DAYS", 14) if days is None else float(days)
    start = _now_ms()
    since = int(start - days * 86400 * 1000)
    old_ms = _f(conn, "BACKFILL_SNOOZE_DAYS", 7) * 86400 * 1000
    known = {q for who in askstore.ask_roles() for q in askstore.asks(who)}
    seen, n, old = set(), 0, []
    for bot in ctx.bots:
        try:
            chans = ctx.api("GET", f"/users/{ctx.ids[bot]}/channels", who=bot)
        except Exception as e:
            log(f"[{ctx.name}] backfill {bot}: channels — {e}")
            continue
        for ch in chans:
            try:
                r = ctx.api("GET", f"/channels/{ch['id']}/posts?since={since}", who=bot)
            except Exception as e:
                log(f"[{ctx.name}] backfill {bot}: channel {ch['id'][:6]} — {e}")
                continue
            for pid in r.get("order", []):
                p = r["posts"][pid]
                pr = p.get("props") or {}
                q = str(pr.get(P("question")) or "")
                if pid in seen or not q.startswith("ask-") or p.get("user_id") != ctx.ids[bot]:
                    continue
                seen.add(pid)
                if q in known or p.get("delete_at") or askstore.ask_state(p) != "open" \
                        or (p.get("create_at") or 0) >= start:
                    continue
                ask = pr.get(P("ask")) or {}
                who = pr.get("agent") or bot
                if q in askstore.asks(who):           # recorded meanwhile — the pass takes minutes
                    known.add(q)
                    continue
                try:
                    p = ctx.api("GET", f"/posts/{pid}", who=bot)
                except Exception:
                    continue
                if p.get("delete_at") or askstore.ask_state(p) != "open":
                    continue
                to = ask.get("to") or ([conn.get("OWNER")] if ch.get("type") == "D" else [])
                askstore.ask_record(who, {"q": q, "post": pid, "via": ctx.name, "thread": p.get("root_id") or pid,
                                          "at": p.get("create_at"), "title": questions.title(ask) or q, "to": to,
                                          "blocking": False, "state": "open", "backfill": True,
                                          **({"sink": ask["sink"]} if ask.get("sink") else {})})
                known.add(q)
                n += 1
                if start - (p.get("create_at") or start) > old_ms:
                    old.append((q, who))
                log(f"[{ctx.name}] backfill {q} ({who})")
    if old:
        with SNOOZE_LOCK:
            path = askstore.snooze_path(ctx.name)
            d = _jload(path, {})
            items = d.setdefault("items", {})
            for q, who in old:
                items.setdefault(q, {"until": None, "by": "backfill", "at": start, "asker": who})
            _jsave(path, d)
    return n
