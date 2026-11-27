"""Slash command registry (commands spec, decision D6).

Mattermost calls only the edge, and the owner's machine is reachable only through its own outbound connection, so a command
runs where its **kind** says:

- ``edge`` — the handler runs in the edge and answers at once (``/unfollow``, ``/bot-token``, ``/agent status|who``);
- ``bridge`` — the edge leaves a signed *command post* in the caller's DM with the target bot; the owner's bridge runs it;
- ``session`` — the same post; the bridge turns it into a ``command`` event in the role's inbox.

Who may call — a set of sender roles (``owner`` / ``peer`` / ``person``), by default ``owner`` only. The role is resolved
here, before any handler runs; a handler cannot widen it. The edge knows only what Mattermost knows: the caller is the
**owner** of a bot when it is the bot's ``owner_id``, a **peer** when the caller is a bot, otherwise a **person**. The bridge
checks the command post again with the full rules (co-owners) before it runs it.

A command post (props ``aimost_command``): {name, sub, args, caller: {id, username}, bot, team_id, channel_id, root_id, at,
kid?, sig}; ``sig`` = HMAC-SHA256(owner key, ``"command|" + canonical JSON without sig``). A post older than ``COMMAND_TTL_S``
is not run silently: the bridge asks the caller first (decided 2026-11-27).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shlex
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from . import heartbeat

if TYPE_CHECKING:  # pragma: no cover
    from .edge import Edge

KINDS = ("edge", "bridge", "session")
ROLES = ("owner", "peer", "person")
COMMAND_TTL_S = 600
FAILED = "Did not work — try again later."


@dataclass
class Call:
    """One slash command call as the edge received it."""
    edge: "Edge"
    name: str
    sub: str
    args: List[str]
    user_id: str
    user_name: str
    team_id: str = ""
    channel_id: str = ""
    root_id: str = ""
    caller: dict = field(default_factory=dict)
    role: str = "person"
    bot: Optional[dict] = None           # the target bot user, when the command names a role
    form: Dict[str, str] = field(default_factory=dict)   # the form Mattermost sent (``response_url`` for a delayed reply)


@dataclass
class Command:
    name: str                             # slash trigger, without "/"
    sub: str                              # subcommand; "" — the trigger takes its own arguments (``/unfollow …``)
    kind: str
    help: str
    args: str = ""                        # shown in help, e.g. "<role> \"<text>\""
    who: Tuple[str, ...] = ("owner",)
    handler: Optional[Callable[[Call], str]] = None   # edge kind only
    role_arg: Optional[int] = None        # index in args of the role whose bot is the target
    min_args: int = 0

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        if not self.who or any(w not in ROLES for w in self.who):
            raise ValueError(f"who must be a non-empty subset of {ROLES}")
        if (self.kind == "edge") != (self.handler is not None):
            raise ValueError("an edge command needs a handler; bridge and session commands run elsewhere")


_REG: Dict[str, Dict[str, Command]] = {}
_builtins_loaded = False


def register(cmd: Command) -> Command:
    subs = _REG.setdefault(cmd.name, {})
    if cmd.sub in subs:
        raise ValueError(f"/{cmd.name} {cmd.sub} is already registered")
    if ("" in subs) or (cmd.sub == "" and subs):
        raise ValueError(f"/{cmd.name}: a trigger either takes its own arguments or has subcommands")
    subs[cmd.sub] = cmd
    return cmd


def _builtins() -> None:
    global _builtins_loaded
    if not _builtins_loaded:
        _builtins_loaded = True
        from . import edgecmds  # noqa: F401  (registers the v1 commands)


def subcommands(name: str) -> Dict[str, Command]:
    _builtins()
    return dict(_REG.get(name) or {})


def names() -> List[str]:
    _builtins()
    return sorted(_REG)


def parse(text: str) -> List[str]:
    try:
        return shlex.split(text or "")
    except ValueError:              # an unbalanced quote: take the words as they are
        return (text or "").split()


def help_text(name: str, role: str) -> str:
    rows = [f"`/{name}{' ' + c.sub if c.sub else ''}{' ' + c.args if c.args else ''}` — {c.help}"
            for c in subcommands(name).values() if role in c.who]
    return "\n".join(rows) if rows else f"No /{name} commands are open to you."


# ── signatures
def slash_key(secret: str, name: str) -> str:
    return hmac.new(secret.encode(), name.encode(), hashlib.sha256).hexdigest()[:32]


def _canon(payload: dict) -> bytes:
    body = json.dumps({k: v for k, v in payload.items() if k != "sig"}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return ("command|" + body).encode()


def sign(payload: dict, key: str) -> str:
    return hmac.new(key.encode(), _canon(payload), hashlib.sha256).hexdigest()


def verify(payload: dict, key: str) -> bool:
    return bool(key) and hmac.compare_digest(str(payload.get("sig") or "").encode(), sign(payload, key).encode())


def registration(public: str, slash_secret: str, team_id: str) -> List[dict]:
    """Bodies for ``POST /api/v4/commands``: one per trigger, URL keyed with the slash secret (edge spec)."""
    from .questions import action_url
    base = action_url({"EDGE_PUBLIC": public})[:-len("/action")]
    out = []
    for name in names():
        subs = subcommands(name)
        hint = " | ".join(s for s in subs if s) or (next(iter(subs.values())).args if subs else "")
        desc = next(iter(subs.values())).help if len(subs) == 1 else f"ai-most: {', '.join(s for s in subs if s)}"
        out.append({"team_id": team_id, "trigger": name, "method": "P", "username": "", "icon_url": "",
                    "url": f"{base}/{name}?k={slash_key(slash_secret, name)}",
                    "auto_complete": True, "auto_complete_desc": desc[:100], "auto_complete_hint": hint[:100],
                    "display_name": f"/{name}", "description": desc[:128]})
    return out


# ── running a call
def _bots(edge: "Edge") -> List[dict]:
    out, page = [], 0
    while True:
        chunk = edge.api("GET", f"/bots?page={page}&per_page=200") or []
        out += chunk
        if len(chunk) < 200:
            return out
        page += 1


def owned_bots(edge: "Edge", owner_id: str) -> List[dict]:
    """Bot users owned by a person, sorted by username."""
    ids = [b["user_id"] for b in _bots(edge) if b.get("owner_id") == owner_id and not b.get("delete_at")]
    users = (edge.api("POST", "/users/ids", ids) or []) if ids else []
    return sorted(users, key=lambda u: u.get("username", ""))


def find_bot(edge: "Edge", role: str, owner_id: Optional[str]) -> Optional[dict]:
    """The bot of a role: a bot with exactly that username, else one of the caller's bots named ``<prefix>-<role>`` or whose
    heartbeat names the role."""
    name = role.lstrip("@").lower()
    try:
        u = edge.api("GET", f"/users/username/{name}")
        if u and u.get("is_bot"):
            return u
    except Exception:
        pass
    if not owner_id:
        return None
    mine = owned_bots(edge, owner_id)
    hit = [u for u in mine if u.get("username", "").endswith("-" + name)
           or (heartbeat.fetch(edge.api, u["id"]) or {}).get("role") == name]
    return hit[0] if len(hit) == 1 else None


def role_of(edge: "Edge", caller: dict, bot: Optional[dict]) -> str:
    """Sender role of the caller towards a target bot (or towards their own bots when no bot is named)."""
    if caller.get("is_bot"):
        return "peer"
    if bot is not None:
        return "owner" if edge.bot_owner(bot["id"]) == caller.get("id") else "person"
    return "owner" if any(b.get("owner_id") == caller.get("id") for b in _bots(edge)) else "person"


def run(edge: "Edge", name: str, form: Dict[str, str]) -> str:
    subs = subcommands(name)
    words = parse(form.get("text", ""))
    try:
        caller = edge.api("GET", f"/users/{form['user_id']}") if form.get("user_id") else {}
    except Exception:
        caller = {}
    if not caller or not caller.get("id"):
        return "Unknown caller — refused."
    if "" in subs:
        cmd, args = subs[""], words
    else:
        sub = words[0].lower() if words else "help"
        if sub == "help" or sub not in subs:
            role = role_of(edge, caller, None)
            prefix = "" if sub == "help" else f"Unknown subcommand `{sub}`.\n"
            return prefix + help_text(name, role)
        cmd, args = subs[sub], words[1:]
    bot = None
    if len(args) < max(cmd.min_args, 0 if cmd.role_arg is None else cmd.role_arg + 1):
        return "Usage: `" + " ".join(x for x in (f"/{name}", cmd.sub, cmd.args) if x) + "`"
    if cmd.role_arg is not None:
        bot = find_bot(edge, args[cmd.role_arg], caller["id"])
        if bot is None:
            return f"No bot for role `{args[cmd.role_arg]}`."
    if caller.get("is_bot"):
        role = "peer"
    elif bot is None and {"owner", "person"} <= set(cmd.who):
        role = "person"                  # allowed either way; the bridge resolves the role again from the caller id
    else:
        role = role_of(edge, caller, bot)
    label = " ".join(x for x in (f"/{name}", cmd.sub) if x)
    if role not in cmd.who:
        return f"`{label}` is not open to you (you are {role} here)."
    call = Call(edge=edge, name=name, sub=cmd.sub, args=args, user_id=caller["id"],
                user_name=caller.get("username") or form.get("user_name", ""), team_id=form.get("team_id", ""),
                channel_id=form.get("channel_id", ""), root_id=form.get("root_id", ""), caller=caller, role=role, bot=bot,
                form=dict(form))
    if cmd.kind == "edge":
        return cmd.handler(call)
    return send_command_post(call, cmd)


def _when(ms: int, now: Optional[float] = None) -> str:
    t = time.localtime(ms / 1000)
    today = time.localtime(time.time() if now is None else now)
    return time.strftime("%H:%M" if t[:3] == today[:3] else "%Y-%m-%d %H:%M", t)


def send_command_post(call: Call, cmd: Command) -> str:
    """Bridge and session kinds: a signed command post in the caller's DM with the target bot."""
    edge = call.edge
    bot = call.bot
    if bot is None:
        # no role named (``/agent waiting``): the caller's bot with the freshest heartbeat — any bot of the owner's bridge
        # reaches it, the bridge reads the DMs of all its bots
        mine = owned_bots(edge, call.user_id)
        if not mine:
            return "You have no bots — there is no bridge to send this to."
        bot = max(mine, key=lambda u: (heartbeat.fetch(edge.api, u["id"]) or {}).get("seen", 0))
    owner_id = edge.bot_owner(bot["id"])
    key = edge.owner_key(owner_id) if owner_id else None
    if not key:
        return f"The owner of @{bot['username']} has no button key on this edge — they issue one with `/bot-token key`."
    payload = {"name": call.name, "sub": call.sub, "args": call.args,
               "caller": {"id": call.user_id, "username": call.user_name}, "bot": bot["id"], "team_id": call.team_id,
               "channel_id": call.channel_id, "root_id": call.root_id, "at": int(time.time() * 1000)}
    if key.get("kid"):
        payload["kid"] = key["kid"]
    payload["sig"] = sign(payload, key["secret"])
    dm = edge.api("POST", "/channels/direct", [call.user_id, bot["id"]])["id"]
    shown = " ".join([f"/{call.name}", call.sub] + [shlex.quote(a) for a in call.args]).replace("  ", " ")
    edge.api("POST", "/posts", {"channel_id": dm, "message": f"`{shown}` — from @{call.user_name}",
                                "props": {"aimost_command": payload}})
    hb = heartbeat.fetch(edge.api, bot["id"])
    if heartbeat.offline(hb):
        since = _when(hb["seen"]) if hb else "never seen"
        return f"Bridge offline since {since} — will deliver to @{bot['username']} when it is back."
    return f"Sent to @{bot['username']}."
