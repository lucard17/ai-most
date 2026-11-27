"""The edge — the HTTP endpoint Mattermost calls: button presses, the "own answer" dialog, slash commands (edge and
commands specs).

Why it exists. Buttons, dialogs and slash commands call a URL the chat server can reach, and the owners' machines are not
reachable from it. So this small server runs next to Mattermost (a container in its compose) and only records the choice
in the props of the post itself (``lmost_answers``: {part: {option | text, by, user_id, at}}); the owner's bridge sees the
change over its outbound connection and decides what counts.

Environment:

    EDGE_MM_URL          Mattermost as the edge sees it (default ``http://app:8065``)
    EDGE_BOT_TOKEN       token of the edge's bot (system admin: it patches posts of other bots, opens dialogs)
    EDGE_LISTEN          listen address (default ``0.0.0.0:8767``)
    EDGE_PUBLIC          the edge as Mattermost sees it, host:port or URL (default ``lmost-edge:8767``, as on the bridge
                         side) — goes into button, dialog and slash URLs
    EDGE_MASTER          master key: owner keys are derived, key(kid) = HMAC(EDGE_MASTER, "kid|gen"); none are stored
    EDGE_KEYS_FILE       kid → {owner_id, owner, gen, created, revoked}, no secrets (default ``/edge-data/edge-keys.json``)
    EDGE_SECRET          shared button key (single-owner setups); a button without ``kid`` is checked against it
    EDGE_OWNER           login whose bots alone may carry buttons signed with EDGE_SECRET
    EDGE_SLASH_SECRET    key of slash command URLs (falls back to EDGE_SECRET)
    EDGE_OWNER_TTL_S     cache of a bot's owner (300)
    EDGE_RELOAD_S        how often the edge checks its own code and re-executes on change (1; 0 — never)
    EDGE_UNFOLLOW_EVERY_S  period of the /unfollow sweep (900; 0 — no sweep thread)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import __version__, commands, confirm, mattermost, questions

Reply = Tuple[int, dict]

RETRY = "Could not read the question from the chat server — press again in a few seconds."
# Mobile Mattermost submits a dialog with the channel open in the app, not the post's channel: from a thread opened via a
# notification the server refuses it and the app shows "submission failed". The edge cannot pick the channel for the
# client — it suggests the way around: an answer in words in the question's thread reaches the session as the owner's.
MOBILE_HINT = "📱 From a phone the dialog may fail to submit — then answer in words in the question's thread."
# props a redraw keeps (a patch replaces props whole): who wrote the post — the session and the relay bot it posted
# through; the pin key (``lmost post --pin``); the "discuss in DM" marks
KEPT_PROPS = ("agent", "relay", "lmost_pin", "lmost_discuss")
SPECIAL_KINDS = ("confirm", "waiting-drop", "waiting-snooze", "discuss")


def gone_text(c: dict) -> str:
    return f"⏹ Question withdrawn: {c.get('why') or 'no longer relevant'} — the answer is not taken. If it still matters, " \
           "write in the thread."


def _now_ms() -> int:
    return int(time.time() * 1000)


class Edge:
    """Configuration, Mattermost access and the request handlers; the HTTP layer is ``make_server``."""

    def __init__(self, env: Optional[Dict[str, str]] = None,
                 call: Optional[Callable[[str, str, object], object]] = None,
                 log: Optional[Callable[[str], None]] = None):
        e = dict(os.environ if env is None else env)
        self.env = e
        self.mm_url = e.get("EDGE_MM_URL", "http://app:8065")
        self.token = e.get("EDGE_BOT_TOKEN", "")
        self.listen = e.get("EDGE_LISTEN", "0.0.0.0:8767")
        self.public = e.get("EDGE_PUBLIC") or "lmost-edge:8767"
        # the same URL the bridge side puts into buttons (questions.action_url); the dialog lives next to it
        self.action_url = questions.action_url({"EDGE_PUBLIC": self.public})
        self.dialog_url = self.action_url[:-len("/action")] + "/dialog"
        self.master = e.get("EDGE_MASTER", "")
        self.secret = e.get("EDGE_SECRET", "")
        self.slash_secret = e.get("EDGE_SLASH_SECRET") or self.secret
        self.owner_login = e.get("EDGE_OWNER", "").strip()
        self.keys_file = Path(e.get("EDGE_KEYS_FILE", "/edge-data/edge-keys.json"))
        self.owner_ttl = float(e.get("EDGE_OWNER_TTL_S", "300"))
        self._conn = {"URL": self.mm_url, "BOT_TOKEN": self.token}
        self._call = call
        self._log = log
        self._keys: dict = {"stamp": None, "data": {}}
        self._owners: Dict[str, Tuple[Optional[str], float]] = {}
        self._edge_owner: Dict[str, str] = {}
        self._me: Optional[str] = None

    # ── plumbing
    def log(self, m: str) -> None:
        if self._log:
            self._log(m)
        else:
            sys.stderr.write("lmost edge: " + m + "\n")
            sys.stderr.flush()

    def api(self, method: str, path: str, body: object = None) -> object:
        if self._call:
            return self._call(method, path, body)
        return mattermost.api(self._conn, method, path, body)

    def me(self) -> str:
        if not self._me:
            self._me = self.api("GET", "/users/me")["id"]
        return self._me

    def check(self) -> Optional[str]:
        """Configuration error that must stop the edge before it listens, or None."""
        if not self.token:
            return "no EDGE_BOT_TOKEN"
        if not self.secret and not self.master:
            return "neither EDGE_SECRET nor EDGE_MASTER"
        if not self.slash_secret:
            return "without EDGE_SECRET, EDGE_SLASH_SECRET is needed"
        if self.owner_login:
            # a typo here would refuse every button without kid — fail at start with a clear message
            try:
                self.edge_owner_id()
            except Exception as x:
                return f"EDGE_OWNER={self.owner_login} not found in Mattermost ({type(x).__name__}) — check the login"
        return None

    # ── owner keys (edge spec: buttons are signed with the bot owner's key)
    def key_for(self, kid: str, gen: int) -> str:
        return hmac.new(self.master.encode(), f"{kid}|{gen}".encode(), hashlib.sha256).hexdigest()

    def keys(self) -> Optional[dict]:
        """kid → record; None — the file exists but cannot be read (every button with a kid is refused). No file — {}."""
        try:
            st = os.stat(self.keys_file)
            stamp: object = (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            stamp = "missing"
        except OSError:
            stamp = "unreadable"
        if stamp == self._keys["stamp"]:
            return self._keys["data"]
        self._keys["stamp"] = stamp
        self._owners.clear()                 # a bot may have been reassigned
        if stamp == "missing":
            self._keys["data"] = {}
            return {}
        try:
            d = json.loads(self.keys_file.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                raise ValueError("not an object")
            self._keys["data"] = d
        except (OSError, ValueError) as x:
            # never fall back to the previous version: a revocation in a broken file would not hold
            self.log(f"keys: keys file not read ({type(x).__name__}) — buttons with kid are refused")
            self._keys["data"] = None
        return self._keys["data"]

    def keys_save(self, d: dict) -> None:
        """Temporary file and rename: a reader never sees a half-written file."""
        self.keys_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(f"{self.keys_file}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.keys_file)

    @staticmethod
    def my_kid(d: dict, uid: str) -> Optional[str]:
        return next((k for k, e in d.items() if isinstance(e, dict) and e.get("owner_id") == uid and not e.get("revoked")),
                    None)

    def owner_key(self, owner_id: str) -> Optional[Dict[str, str]]:
        """The signature an owner's bots carry: their live key, else the shared key if that owner may use it."""
        d = self.keys() if self.master else None
        kid = self.my_kid(d, owner_id) if d else None
        if kid:
            return {"secret": self.key_for(kid, d[kid].get("gen", 1)), "kid": kid}
        if self.secret:
            try:
                eo = self.edge_owner_id()
            except Exception:
                return None
            if not eo or eo == owner_id:
                return {"secret": self.secret}
        return None

    def bot_owner(self, uid: str) -> Optional[str]:
        """Owner id of a bot, None — not a bot; cached for EDGE_OWNER_TTL_S."""
        hit = self._owners.get(uid)
        if hit and time.time() - hit[1] < self.owner_ttl:
            return hit[0]
        try:
            owner = (self.api("GET", f"/bots/{uid}") or {}).get("owner_id")
        except urllib.error.HTTPError as x:
            if x.code != 404:
                raise
            owner = None
        self._owners[uid] = (owner, time.time())
        return owner

    def post_owner(self, post_id: Optional[str]) -> Optional[str]:
        """Owner of the bot that wrote the post; None — a person's post or not readable."""
        if not post_id:
            return None
        try:
            uid = (self.api("GET", f"/posts/{post_id}") or {}).get("user_id")
        except Exception as x:
            self.log(f"post {post_id}: not read ({type(x).__name__})")
            return None
        return self.bot_owner(uid) if uid else None

    def edge_owner_id(self) -> Optional[str]:
        """Id of EDGE_OWNER; a read failure raises — the caller refuses rather than lets through."""
        if not self.owner_login:
            return None
        if self.owner_login not in self._edge_owner:
            self._edge_owner[self.owner_login] = self.api(
                "GET", f"/users/username/{urllib.parse.quote(self.owner_login)}")["id"]
        return self._edge_owner[self.owner_login]

    def button_auth(self, ctx: dict, post_id: Optional[str], trusted: bool = False) -> Optional[Dict[str, str]]:
        """Check a button → the signature to redraw the post with, or None (refused). ``trusted`` — the key was already
        checked (dialog state signed by the edge): only revocation and the post's owner are checked."""
        kid = ctx.get("kid")
        if kid:
            d = self.keys()
            if d is None or not self.master:
                return None
            e = d.get(str(kid))
            if not isinstance(e, dict) or e.get("revoked"):
                self.log(f"keys: kid {kid} unknown or revoked")
                return None
            # a record without an owner (hand-edited file) is refused: otherwise "post owner None == key owner None"
            # would pass a button on a person's post or on an unreadable post
            if not e.get("owner_id"):
                self.log(f"keys: kid {kid} — record has no owner")
                return None
            key = self.key_for(kid, e.get("gen", 1))
            if not trusted and not hmac.compare_digest(str(ctx.get("secret") or "").encode(), key.encode()):
                self.log(f"keys: kid {kid} — key does not match")
                return None
            if self.post_owner(post_id) != e.get("owner_id"):
                self.log(f"keys: kid {kid} — post {post_id} is not by the key owner's bot")
                return None
            return {"secret": key, "kid": kid}
        if not self.secret or not (trusted or hmac.compare_digest(str(ctx.get("secret") or "").encode(),
                                                                  self.secret.encode())):
            return None
        try:
            eo = self.edge_owner_id()
        except Exception as x:
            self.log(f"keys: EDGE_OWNER not read ({type(x).__name__}) — button without kid refused")
            return None
        if eo and self.post_owner(post_id) != eo:
            self.log(f"keys: button without kid on post {post_id} not by a bot of EDGE_OWNER")
            return None
        return {"secret": self.secret}

    def state_sig(self, st: dict) -> str:
        """Signature of the dialog state: Mattermost gives the state to the presser's client and returns it on submit, so it
        carries no key; the signature stops swapping the post, part or question."""
        body = json.dumps({k: v for k, v in st.items() if k != "sig"}, sort_keys=True, ensure_ascii=False)
        key = hmac.new(self.master.encode(), b"dialog", hashlib.sha256).hexdigest() if self.master else self.secret
        return hmac.new(key.encode(), ("dialog|" + body).encode(), hashlib.sha256).hexdigest()

    # ── questions
    def question_state(self, post_id: Optional[str], ctx: dict):
        """(spec, answers for render, marks, cancelled, post props) from the post's props. Spec None — the post was not
        read (the press is refused with "press again")."""
        props: dict = {}
        if post_id:
            try:
                props = (self.api("GET", f"/posts/{post_id}") or {}).get("props") or {}
            except Exception as x:
                self.log(f"post {post_id}: not read ({type(x).__name__})")
        spec = props.get("lmost_ask") or props.get("lmost_spec") or ctx.get("spec")
        marks = dict(props.get("lmost_answers") or ctx.get("marks") or {})
        answers = dict(ctx.get("answers") or {})
        for k, m in marks.items():
            if m.get("by"):
                answers[k] = {**({"option": m["option"]} if m.get("option") else {"text": m.get("text", "")}), "by": m["by"]}
        return spec, answers, marks, props.get("lmost_cancelled"), props

    def props_for(self, ctx: dict, spec: dict, answers: dict, marks: dict, post_props: dict, auth: Dict[str, str]):
        """Message and props of a question post after a choice. A patch replaces props whole — the spec (read by the bridge
        and the next press) and the author are written again."""
        msg, atts = questions.render(ctx["q"], spec, answers, ctx.get("agent"), auth, self.action_url)
        props = {"attachments": atts, "lmost_question": ctx["q"], "lmost_answers": marks,
                 ("lmost_ask" if ctx["q"].startswith("ask-") else "lmost_spec"): spec}
        props.update({k: v for k, v in (post_props or {}).items() if k in KEPT_PROPS and v})
        if ctx.get("agent"):
            props["agent"] = ctx["agent"]
        return msg, props

    def username(self, uid: str) -> str:
        return self.api("GET", f"/users/{uid}")["username"]

    def handle_dialog(self, body: dict) -> Reply:
        try:
            st = json.loads(body.get("state") or "{}")
        except ValueError:
            st = {}
        if not isinstance(st, dict) or not hmac.compare_digest(str(st.get("sig") or "").encode(),
                                                               self.state_sig(st).encode()):
            return 403, {}
        auth = self.button_auth(st, st.get("post_id"), trusted=True)   # key checked on open; revocation and owner — now
        if not auth:
            return 403, {}
        spec, answers, marks, c, pprops = self.question_state(st.get("post_id"), st)
        if not spec:
            return 200, {"errors": {"answer": RETRY}}
        user = self.username(body["user_id"])
        to = spec.get("to")
        if to and user not in to:
            return 200, {"errors": {"answer": f"the question is for {', '.join('@' + x for x in to)}"}}
        if c:
            return 200, {"errors": {"answer": gone_text(c)}}
        text = ((body.get("submission") or {}).get("answer") or "").strip()
        if not text:
            return 200, {"errors": {"answer": "empty answer"}}
        part = str(st["part"])
        answers[part] = {"text": text, "by": user}
        marks[part] = {"text": text, "by": user, "user_id": body["user_id"], "at": _now_ms()}
        m, props = self.props_for(st, spec, answers, marks, pprops, auth)
        self.api("PUT", f"/posts/{st['post_id']}/patch", {"message": m, "props": props})
        return 200, {}

    def _patch_marks(self, post_id: str, key: str, name: str, mark: dict) -> None:
        # fresh props from the server: the button context may be stale, and a patch replaces props whole
        post = self.api("GET", f"/posts/{post_id}") or {}
        props = dict(post.get("props") or {})
        marks = dict(props.get(key) or {})
        marks[name] = mark
        props[key] = marks
        self.api("PUT", f"/posts/{post['id']}/patch", {"props": props})

    def handle_action(self, body: dict) -> Reply:
        ctx = body.get("context") or {}
        post_id = body.get("post_id")
        auth = self.button_auth(ctx, post_id)
        if not auth:
            return 403, {"ephemeral_text": "lmost edge: foreign request"}
        uid = body["user_id"]
        user = self.username(uid)
        kind = ctx.get("kind")
        special = kind in SPECIAL_KINDS
        if not special:
            spec, answers, marks, c, pprops = self.question_state(post_id, ctx)
            if not spec:
                return 200, {"ephemeral_text": RETRY}
        to = ctx.get("to") if special else spec.get("to")
        if to and user not in to:
            if kind == "confirm":
                return 200, {"ephemeral_text": f"Confirmed by {' or '.join('@' + x for x in to)} — your press does not "
                                               "count. Write your opinion in the thread."}
            return 200, {"ephemeral_text": f"This question is for {', '.join('@' + x for x in to)}: buttons are taken "
                                           "only from the addressee. Write your opinion in the thread."}
        if kind == "waiting-drop":
            # "no longer relevant" in the "Waiting for you" pin: a mark in the pin's props; the owner's bridge drops it
            self._patch_marks(post_id, "lmost_waiting_drop", ctx["q"],
                              {"asker": ctx.get("asker"), "user_id": uid, "at": _now_ms()})
            return 200, {"ephemeral_text": f"Dropping {ctx['q']} — the pin updates in a few seconds."}
        if kind == "waiting-snooze":
            v = str(ctx.get("selected_option") or "")
            if not v:
                return 200, {"ephemeral_text": "No period chosen."}
            self._patch_marks(post_id, "lmost_waiting_snooze", ctx.get("key") or v,
                              {"value": v, "asker": ctx.get("asker"), "user_id": uid, "at": _now_ms()})
            return 200, {"ephemeral_text": ("Bringing back" if v.startswith("back:") else "Snoozing")
                         + " — the pin updates in a few seconds."}
        if kind == "discuss":
            # a mark in the post's props; the DM thread, the moved line and the session's event are made by the owner's
            # bridge — the edge has no session bot tokens
            post = self.api("GET", f"/posts/{post_id}") or {}
            props = dict(post.get("props") or {})
            if props.get("lmost_cancelled"):
                return 200, {"ephemeral_text": gone_text(props["lmost_cancelled"])}
            dm = dict(props.get("lmost_discuss") or {})
            if uid in dm:
                return 200, {"ephemeral_text": "The discussion is already open — in your DM with the session's bot."}
            dm[uid] = {"at": _now_ms(), "by": user}
            props["lmost_discuss"] = dm
            self.api("PUT", f"/posts/{post['id']}/patch", {"props": props})
            return 200, {"ephemeral_text": "💬 The discussion opens in your DM with the session's bot in a few seconds."}
        if kind == "confirm":
            done = {"answer": ctx["answer"], "by": user, "user_id": uid, "id": ctx["id"], "at": _now_ms()}
            msg, atts = confirm.render_confirm(ctx["id"], ctx["action"], ctx.get("details") or "", ctx.get("agent"),
                                               self.action_url, auth, done=done)
            props = {"attachments": atts, "lmost_confirm": done, "lmost_confirm_request": ctx["id"]}
            if ctx.get("agent"):
                props["agent"] = ctx["agent"]
            return 200, {"update": {"message": msg, "props": props}}
        if c:
            return 200, {"ephemeral_text": gone_text(c)}
        q, n = ctx["q"], int(ctx["part"])
        if ctx.get("custom"):
            part = spec["parts"][n - 1]
            # the button key never goes into the state: the presser's browser sees it
            state = {k: v for k, v in ctx.items() if k not in ("spec", "answers", "marks", "secret", "sig")}
            state["post_id"] = post_id
            if not post_id:
                state.update(spec=spec, answers=answers, marks=marks)
            state["sig"] = self.state_sig(state)
            self.api("POST", "/actions/dialogs/open", {
                "trigger_id": body.get("trigger_id"), "url": self.dialog_url,
                "dialog": {"callback_id": "custom", "title": f"{q} · {part['title']}"[:24], "submit_label": "Answer",
                           "introduction_text": (part["text"] + "\n\n" if part.get("text") else "") + MOBILE_HINT,
                           "state": json.dumps(state),
                           "elements": [{"display_name": "Answer", "name": "answer", "type": "textarea",
                                         "max_length": 2000}]}})
            return 200, {"ephemeral_text": MOBILE_HINT}
        m = int(ctx["option"])
        answers[str(n)] = {"option": m, "by": user}
        marks[str(n)] = {"option": m, "by": user, "user_id": uid, "at": _now_ms()}
        text, props = self.props_for(ctx, spec, answers, marks, pprops, auth)
        return 200, {"update": {"message": text, "props": props}}

    # ── slash commands (commands spec)
    def slash_key(self, name: str) -> str:
        return commands.slash_key(self.slash_secret, name)

    def handle_slash(self, name: str, query: str, form: Dict[str, str]) -> Reply:
        if not commands.subcommands(name):
            return 404, {}
        k = (urllib.parse.parse_qs(query).get("k") or [""])[0]
        if not hmac.compare_digest(k.encode(), self.slash_key(name).encode()):
            return 403, {}
        try:
            text = commands.run(self, name, form)
        except Exception as x:
            self.log(f"{name}: command failed: {type(x).__name__}")
            text = commands.FAILED
        return 200, {"response_type": "ephemeral", "text": text}

    def handle(self, path: str, raw: bytes) -> Reply:
        p = urllib.parse.urlsplit(path)
        name = p.path.lstrip("/")
        if commands.subcommands(name):
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode(), keep_blank_values=True).items()}
            return self.handle_slash(name, p.query, form)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return 400, {}
        if not isinstance(body, dict):
            return 400, {}
        if p.path == "/dialog":
            return self.handle_dialog(body)
        if p.path == "/action":
            return self.handle_action(body)
        return 404, {}


# ── HTTP layer, with re-exec on a code change: an edge container mounting the package would otherwise keep serving the code
# it started with (presses ending in "integration error" after an upgrade). A file that does not compile is not run.
def _code_stamp() -> Tuple:
    pkg = Path(__file__).resolve().parent
    out = []
    for f in sorted(pkg.glob("*.py")):
        try:
            st = f.stat()
            out.append((f.name, st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    return tuple(out)


def _compiles() -> Optional[str]:
    for f in sorted(Path(__file__).resolve().parent.glob("*.py")):
        try:
            compile(f.read_text(encoding="utf-8"), str(f), "exec")
        except (SyntaxError, ValueError, OSError) as x:
            return f"{f.name}: {type(x).__name__}"
    return None


def make_server(edge: Edge, reload_s: Optional[float] = None, argv: Optional[List[str]] = None) -> HTTPServer:
    host, port = edge.listen.rsplit(":", 1)
    reload_s = float(edge.env.get("EDGE_RELOAD_S", "1")) if reload_s is None else reload_s
    argv = list(sys.argv[1:] if argv is None else argv)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/version":
                return self.reply(200, {"version": __version__})
            self.reply(404, {})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            try:
                code, obj = edge.handle(self.path, raw)
            except Exception as x:
                edge.log(f"{urllib.parse.urlsplit(self.path).path}: {type(x).__name__}")
                code, obj = 500, {}
            self.reply(code, obj)

        def reply(self, code: int, obj: dict) -> None:
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, fmt, *a):
            edge.log(re.sub(r"\?k=[0-9a-f]+", "?k=…", fmt % a))   # the slash URL key never goes into the log

    class Server(HTTPServer):
        started = _code_stamp()
        checked = 0.0
        bad: Optional[Tuple] = None
        bad_err: Optional[str] = None

        def service_actions(self):
            if reload_s <= 0:
                return
            now = time.time()
            if now - self.checked < reload_s:
                return
            self.checked = now
            cur = _code_stamp()
            if cur == self.started or cur == self.bad:
                return
            err = _compiles()
            if err:
                self.bad = cur
                if err == self.bad_err:          # the same breakage, written in several steps — said once
                    return
                self.bad_err = err
                edge.log(f"code changed but does not compile ({err}) — serving the previous code")
                return
            edge.log("code changed — re-executing")
            self.server_close()                  # free the port before execv: the new process listens on it
            try:
                os.execv(sys.executable, [sys.executable, "-u", "-m", "lmost"] + argv)
            except OSError as x:
                edge.log(f"execv failed ({x}) — exiting, the container restarts")
                sys.exit(1)

    return Server((host, int(port)), Handler)


def serve(edge: Edge) -> None:
    from . import edgecmds
    every = float(edge.env.get("EDGE_UNFOLLOW_EVERY_S", "900"))
    if every > 0:
        threading.Thread(target=edgecmds.unfollow_loop, args=(edge, every), daemon=True).start()
    edge.log(f"listening on {edge.listen}, Mattermost {edge.mm_url}, buttons call {edge.public}, version {__version__}")
    make_server(edge).serve_forever()
