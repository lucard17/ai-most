"""Built-in slash commands of v1 (commands spec): edge handlers and the declarations of bridge / session commands.

- ``/unfollow`` — a person unfollows quiet threads of chosen bots automatically. Settings are the person's own preferences
  (``lmost_unfollow/<bot id>`` = {hours}), history — ``lmost_unfollow_log/<thread>`` = {bot, at}; who enabled it — the edge
  bot's own list (``lmost_unfollow_users/<user id>`` = {team}), otherwise there is nobody to sweep. The sweep unfollows a
  thread whose root the chosen bot wrote, last reply older than the period, no unread mentions. A new mention follows again.
- ``/bot-token`` — the owner of a bot issues its token (ephemeral answer only the caller sees; the value goes into no log or
  file) or revokes all of them; ``/bot-token key`` — the owner's button key (edge spec).
- ``/agent status [role]``, ``/agent who`` — answered from the bots' heartbeat (status spec), without the owner's machine.
- ``/agent waiting``, ``/agent mute|follow <role>`` (bridge), ``/agent ask <role> "<text>"`` (session) — command posts.
"""

from __future__ import annotations

import json
import re
import secrets
import time
import urllib.error
import urllib.parse
from typing import TYPE_CHECKING, List, Optional

from . import heartbeat
from .commands import Call, Command, owned_bots, register

if TYPE_CHECKING:  # pragma: no cover
    from .edge import Edge

# ── /unfollow
UNF_CAT, UNF_LOG, UNF_USERS = "lmost_unfollow", "lmost_unfollow_log", "lmost_unfollow_users"
UNF_DEFAULT_H, UNF_MIN_H, UNF_MAX_H, UNF_KEEP_S = 2.0, 0.5, 24.0 * 7, 14 * 86400
UNF_HELP = ("`/unfollow @<bot> [period]` — unfollow that bot's threads after a quiet period (`30m`, `2h`, `1d`; default 2h, "
            "from 30 minutes to 7 days) · `/unfollow @<bot> off` — turn off · `/unfollow` — my settings · "
            "`/unfollow list` — threads you were unfollowed from in 14 days")


def parse_hours(t: str) -> Optional[float]:
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(m|min|h|d)?", t.strip().lower())
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    u = m.group(2) or "h"
    return v / 60 if u in ("m", "min") else v * 24 if u == "d" else v


def human_h(h: float) -> str:
    return f"{int(h * 60)} min" if h < 1 else f"{h:g} h" if h < 48 else f"{h / 24:g} d"


def prefs_get(edge: "Edge", uid: str, cat: str) -> dict:
    if not uid:
        return {}
    try:
        rows = edge.api("GET", f"/users/{uid}/preferences/{cat}") or []
    except urllib.error.HTTPError as x:
        if x.code == 404:
            return {}
        raise
    return {p["name"]: json.loads(p.get("value") or "{}") for p in rows}


def pref_put(edge: "Edge", uid: str, cat: str, name: str, value: dict) -> None:
    edge.api("PUT", f"/users/{uid}/preferences", [{"user_id": uid, "category": cat, "name": name, "value": json.dumps(value)}])


def pref_del(edge: "Edge", uid: str, cat: str, names: List[str]) -> None:
    if names:
        edge.api("POST", f"/users/{uid}/preferences/delete", [{"user_id": uid, "category": cat, "name": n} for n in names])


def site_url(edge: "Edge") -> str:
    try:
        return (edge.api("GET", "/config/client?format=old") or {}).get("SiteURL", "").rstrip("/")
    except Exception:
        return ""


def _uname(edge: "Edge", uid: str) -> str:
    try:
        return edge.api("GET", f"/users/{uid}")["username"]
    except Exception:
        return uid


def unfollow(call: Call) -> str:
    edge, uid, team, args = call.edge, call.user_id, call.team_id, call.args
    me = edge.me()
    if not args:
        mine = prefs_get(edge, uid, UNF_CAT)
        if not mine:
            return "No auto-unfollow.\n" + UNF_HELP
        rows = [f"- @{_uname(edge, bid)} — after {human_h(float(v.get('hours', UNF_DEFAULT_H)))} of quiet"
                for bid, v in mine.items()]
        return "Auto-unfollow from threads:\n" + "\n".join(rows) + "\n\n" + UNF_HELP
    if args[0] == "help":
        return UNF_HELP
    if args[0] == "list":
        now = time.time()
        hist = [(t, v) for t, v in sorted(prefs_get(edge, uid, UNF_LOG).items(), key=lambda kv: -kv[1].get("at", 0))
                if now - v.get("at", 0) < UNF_KEEP_S]
        if not hist:
            return "No unfollows in 14 days."
        site = site_url(edge)
        tn = edge.api("GET", f"/teams/{team}")["name"] if team else ""
        return "Unfollowed in 14 days:\n" + "\n".join(
            f"- {time.strftime('%Y-%m-%d %H:%M', time.localtime(v['at']))} · @{v.get('bot', '?')} · {site}/{tn}/pl/{t}"
            for t, v in hist)
    name = args[0].lstrip("@").lower()
    try:
        bot = edge.api("GET", f"/users/username/{urllib.parse.quote(name)}")
    except urllib.error.HTTPError:
        return f"No user @{name}.\n" + UNF_HELP
    if not bot.get("is_bot"):
        return f"@{name} is not a bot: unfollow works for bot threads only.\n" + UNF_HELP
    if len(args) > 1 and args[1].lower() in ("off", "no"):
        pref_del(edge, uid, UNF_CAT, [bot["id"]])
        if not prefs_get(edge, uid, UNF_CAT):
            pref_del(edge, me, UNF_USERS, [uid])
        edge.log(f"unfollow: {uid} — off for @{name}")
        return f"Auto-unfollow from @{name} threads is off."
    h = UNF_DEFAULT_H if len(args) < 2 else parse_hours(args[1])
    if h is None or not UNF_MIN_H <= h <= UNF_MAX_H:
        return f"Period “{args[1] if len(args) > 1 else ''}” not understood or out of bounds (30 minutes to 7 days).\n" \
               + UNF_HELP
    pref_put(edge, uid, UNF_CAT, bot["id"], {"hours": h})
    pref_put(edge, me, UNF_USERS, uid, {"team": team})
    edge.log(f"unfollow: {uid} — on for @{name}, {h:g} h")
    return f"On: unfollowing you from @{name} threads with no messages for {human_h(h)}. Settings — `/unfollow`, " \
           "history — `/unfollow list`."


def unfollow_user(edge: "Edge", uid: str, team: str, now: Optional[float] = None) -> int:
    """One person: unfollow quiet threads of the chosen bots, trim the history. → number of unfollows."""
    now = now or time.time()
    mine = prefs_get(edge, uid, UNF_CAT)
    if not mine:
        return 0
    page, done = 0, []
    while True:
        r = edge.api("GET", f"/users/{uid}/teams/{team}/threads?per_page=100&page={page}&extended=false") or {}
        th = r.get("threads") or []
        for t in th:
            post = t.get("post") or {}
            bot = post.get("user_id")
            if bot not in mine or t.get("unread_mentions"):
                continue
            last = (t.get("last_reply_at") or post.get("create_at") or 0) / 1000
            if now - last < float(mine[bot].get("hours", UNF_DEFAULT_H)) * 3600:
                continue
            try:
                edge.api("DELETE", f"/users/{uid}/teams/{team}/threads/{t['id']}/following")
                done.append((t["id"], bot))
                edge.log(f"unfollow: {uid} — unfollowed {t['id']} (bot {bot}, quiet {(now - last) / 3600:.1f} h)")
            except Exception as x:
                edge.log(f"unfollow: {uid} thread {t.get('id')}: {type(x).__name__}")
        if len(th) < 100:
            break
        page += 1
    names = {}
    for tid, bot in done:
        if bot not in names:
            names[bot] = _uname(edge, bot)
        pref_put(edge, uid, UNF_LOG, tid, {"bot": names[bot], "at": int(now)})
    pref_del(edge, uid, UNF_LOG, [k for k, v in prefs_get(edge, uid, UNF_LOG).items() if now - v.get("at", 0) >= UNF_KEEP_S])
    return len(done)


def unfollow_sweep(edge: "Edge") -> None:
    for uid, v in prefs_get(edge, edge.me(), UNF_USERS).items():
        try:
            unfollow_user(edge, uid, v.get("team", ""))
        except Exception as x:
            edge.log(f"unfollow: sweep {uid}: {type(x).__name__}")


def unfollow_loop(edge: "Edge", every_s: float) -> None:
    while True:
        time.sleep(every_s)
        try:
            unfollow_sweep(edge)
        except Exception as x:
            edge.log(f"unfollow: sweep: {type(x).__name__}")


# ── /bot-token
BT_HELP = ("`/bot-token <bot>` — issue a new token of your bot (only you see it, copy it at once) · "
           "`/bot-token <bot> revoke` — revoke all tokens of the bot · "
           "`/bot-token key` — a new personal button key (the previous one stops working) · "
           "`/bot-token key revoke` — revoke the key")


def owns_bots(edge: "Edge", uid: str) -> bool:
    page = 0
    while True:
        chunk = edge.api("GET", f"/bots?page={page}&per_page=200") or []
        if any(b.get("owner_id") == uid for b in chunk):
            return True
        if len(chunk) < 200:
            return False
        page += 1


def key_text(edge: "Edge", kid: str, gen: int, fresh: bool) -> str:
    return (("Personal button key — " if fresh else "New personal button key (the previous one no longer works) — ")
            + "put it into your bridge connection (`~/.config/lmost/connections/<connection>.env`, mode 600):\n"
            f"```\nEDGE_KEY_ID={kid}\nEDGE_SECRET={edge.key_for(kid, gen)}\n```\n"
            "It signs buttons on posts of your bots only. Lost or leaked — `/bot-token key`.")


def key_command(edge: "Edge", uid: str, who: str, args: List[str]) -> str:
    """``/bot-token key [revoke]`` — a new generation of the owner's key, or its revocation."""
    if not edge.master:
        return "Personal button keys are not enabled on this server (no EDGE_MASTER)."
    cur = edge.keys()
    if cur is None:
        return "The keys file cannot be read — ask the edge admin to check it. No key issued."
    # edit a copy: the cache changes only after a successful write, otherwise a revocation would live in memory only
    d = json.loads(json.dumps(cur))
    kid = edge.my_kid(d, uid)

    def save(what: str) -> bool:
        try:
            edge.keys_save(d)
            edge.keys()
            return True
        except OSError as x:
            edge.log(f"keys: {what} not written ({type(x).__name__}), kid {kid}")
            return False

    if len(args) > 1 and args[1] == "revoke":
        # revocation works without bots too: they may have been taken away, the key must still go
        if not kid:
            return "No live key."
        d[kid]["revoked"] = int(time.time())
        if not save("revocation"):
            return "⚠️ Revocation NOT taken: the keys file was not written. The key still works — tell the edge admin."
        edge.log(f"keys: key revoked, kid {kid}, owner {who}")
        return "Key revoked: buttons with it are no longer accepted. A new one — `/bot-token key`."
    if not owns_bots(edge, uid):
        return "You have no bots of your own — no button key needed."
    fresh = not kid
    if fresh:
        kid = "k" + secrets.token_hex(4)
        d[kid] = {"owner_id": uid, "owner": who, "gen": 1, "created": int(time.time())}
    else:
        d[kid]["gen"] = int(d[kid].get("gen", 1)) + 1
        d[kid]["created"] = int(time.time())
    if not save("issue"):
        return "⚠️ No key issued: the keys file was not written. The previous key still works — tell the edge admin."
    edge.log(f"keys: key issued, kid {kid}, generation {d[kid]['gen']}, owner {who}")
    return key_text(edge, kid, d[kid]["gen"], fresh)


def bot_token(call: Call) -> str:
    edge, uid, who, args = call.edge, call.user_id, call.user_name or "?", call.args
    if not args or args[0] == "help":
        return BT_HELP
    # the caller and the owner are people: a bot owning a bot would hand the token to an agent
    if call.caller.get("is_bot"):
        return "A bot token is issued only by its human owner — a call from a bot is refused."
    if args[0] == "key":
        return key_command(edge, uid, who, args)   # a bot named "key" — `/bot-token @key`
    name = args[0].lstrip("@").lower()
    try:
        bot = edge.api("GET", f"/users/username/{urllib.parse.quote(name)}")
    except urllib.error.HTTPError:
        return f"No user @{name}.\n" + BT_HELP
    if not bot.get("is_bot"):
        return f"@{name} is not a bot: people's tokens are not issued this way."
    owner = (edge.api("GET", f"/bots/{bot['id']}") or {}).get("owner_id")
    try:
        owner_user = edge.api("GET", f"/users/{owner}") if owner and owner != bot["id"] else {}
    except Exception:
        owner_user = {}
    if not owner_user.get("id") or owner_user.get("is_bot"):
        return f"@{name} has no human owner — ask a chat admin to assign you (`mmctl bot assign`)."
    if owner != uid:
        return f"The owner of @{name} is @{owner_user.get('username', owner)}: only they issue its token."
    if len(args) > 1 and args[1] == "revoke":
        # all pages first, then revoke: revoking mid-walk would shift the pages. The log gets numbers only
        ids, page = [], 0
        while True:
            chunk = edge.api("GET", f"/users/{bot['id']}/tokens?page={page}&per_page=200") or []
            if not chunk:
                break
            ids += [t["id"] for t in chunk]
            page += 1
        edge.log(f"bot-token: revoking {len(ids)} token(s) of @{name} by {who}")
        done = 0
        try:
            for i in ids:
                edge.api("POST", "/users/tokens/revoke", {"token_id": i})
                done += 1
        finally:
            edge.log(f"bot-token: revoked {done} of {len(ids)} of @{name}")
        return f"Tokens of @{name} revoked: {done}. A new one — `/bot-token {name}`."
    t = edge.api("POST", f"/users/{bot['id']}/tokens", {"description": f"/bot-token {who} {time.strftime('%Y-%m-%d %H:%M')}"})
    edge.log(f"bot-token: issued for @{name} by {who}, id {t.get('id')}")
    text = (f"Token of @{name} — copy it now: only you see this message, and it is gone after a reload.\n"
            f"```\n{t['token']}\n```\nWhere: `~/.config/lmost/bots/<connection>/<role>`, mode 600. "
            f"Old tokens keep working until revoked: `/bot-token {name} revoke`, then issue a new one.")
    # the first button key comes in the same answer; an existing one is not shown again — only by `/bot-token key`
    d = edge.keys() if edge.master else None
    if d is not None and not edge.my_kid(d, uid):
        text += "\n\n" + key_command(edge, uid, who, ["key"])
    return text


# ── /agent status | who — from the heartbeat
def _age(s: float) -> str:
    return f"{int(s)} s" if s < 90 else f"{int(s // 60)} min" if s < 5400 else f"{s / 3600:.1f} h"


def _when(ms: Optional[int], now: float) -> str:
    if not ms:
        return "—"
    t = time.localtime(ms / 1000)
    return time.strftime("%H:%M" if t[:3] == time.localtime(now)[:3] else "%Y-%m-%d %H:%M", t)


def _row(u: dict, hb: Optional[dict], now: float) -> str:
    if not hb:
        return f"- @{u['username']} — no heartbeat yet"
    return (f"- @{u['username']}" + (f" ({hb['role']})" if hb.get("role") and hb["role"] != u["username"] else "")
            + f" — {hb['state']} · unread {hb['unread']} · last turn {_when(hb.get('last_turn'), now)}")


def status_text(edge: "Edge", bots: List[dict], now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    if not bots:
        return "You have no bots."
    beats = [(u, heartbeat.fetch(edge.api, u["id"])) for u in bots]
    newest = max((hb["seen"] for _, hb in beats if hb), default=None)
    rows = [_row(u, hb, now) for u, hb in beats]
    if newest is None:
        return "Bridge never seen: no heartbeat from your bots.\n" + "\n".join(rows)
    age = now - newest / 1000
    if age > heartbeat.STALE_S:
        return f"Bridge offline since {_when(newest, now)} — last known state:\n" + "\n".join(rows)
    return "\n".join(rows) + f"\n_data {_age(max(0.0, age))} old_"


def agent_who(call: Call) -> str:
    return status_text(call.edge, owned_bots(call.edge, call.user_id))


def _status(call: Call) -> str:
    if call.args:                   # `/agent status <role>` — resolved here, the role arg is optional
        from .commands import find_bot
        bot = find_bot(call.edge, call.args[0], call.user_id)
        if bot is None:
            return f"No bot for role `{call.args[0]}`."
        if call.edge.bot_owner(bot["id"]) != call.user_id:
            return f"@{bot['username']} is not your bot."
        return status_text(call.edge, [bot])
    return status_text(call.edge, owned_bots(call.edge, call.user_id))


register(Command("unfollow", "", "edge", "unfollow quiet bot threads", "[@<bot> [period|off] | list]",
                 who=("owner", "person"), handler=unfollow))
register(Command("bot-token", "", "edge", "issue or revoke your bot's token or button key", "<bot> [revoke] | key [revoke]",
                 who=("owner", "person"), handler=bot_token))
register(Command("agent", "status", "edge", "state of your roles (or one), from the bridge heartbeat", "[role]",
                 handler=_status))
register(Command("agent", "who", "edge", "your roles with state and unread count", handler=agent_who))
register(Command("agent", "waiting", "bridge", "the “Waiting for you” list"))
register(Command("agent", "mute", "bridge", "mute the role in this thread", "<role>", role_arg=0))
register(Command("agent", "follow", "bridge", "the role follows this thread again", "<role>", role_arg=0))
register(Command("agent", "ask", "session", "ask a role; arrives in its inbox", "<role> \"<text>\"",
                 who=("owner", "person"), role_arg=0, min_args=2))
