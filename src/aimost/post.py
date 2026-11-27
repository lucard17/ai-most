"""Posting as a role's bot (specs: text, questions; delivery → relay bot).

A role posts with its own bot (``connections.bot``): the author of the post is real, so mentions and threads work. An
admitted role without a bot may post through the connection's relay bot — only into the owner's direct message and its
own threads there, every post signed "from session <role> · via relay <bot>" in the first line.

Where a post goes:
- ``channel="dm"`` (default) — the direct message with the owner; ``dm:<login>`` — with that person; any other value —
  a channel of the connection's team by name;
- with ``thread`` and no explicit channel — the thread's own channel;
- no ``thread`` during a chat-started turn (activity state of the role) — into that turn's thread, unless ``new_thread``.

Every outgoing text goes through ``textcheck.run`` first; a refusal is ``PostError`` with the check's exit code.
Exit codes: 2 usage, 3 not admitted / no bot / someone else's post, 4–7 text checks, 8 the bot is not in the channel,
20–39 plugin text checks.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import askstore, connections, inbox, mattermost, paths, questions, textcheck
from .names import P


class PostError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def role_name() -> Optional[str]:
    return inbox.session_name()


def turn_state(role: str) -> Optional[dict]:
    """The role's running chat turn (``aimost activity``): via, root, from, role…"""
    from . import activity
    return activity.turn(role)


def _mermaid_dir() -> Path:
    return Path(os.environ.get("AIMOST_MERMAID_DIR") or Path(os.path.expanduser("~")) / ".cache" / "aimost" / "mermaid")


MERMAID = re.compile(r"^```mermaid[^\n]*\n(.*?)^```[ \t]*$", re.S | re.M)


def mermaid_images(text: str, warn: Callable[[str], None] = lambda m: None) -> List[str]:
    """PNG files of the ```mermaid blocks of text. Desktop clients draw them with a server plugin, phones show the code —
    so the images go into the thread as well. Renderer: ``AIMOST_MERMAID_BIN <in.mmd> <out.png>``, else mermaid-cli in
    Docker. A diagram that fails to render is skipped with a warning."""
    pngs = []
    for n, m in enumerate(MERMAID.finditer(text or ""), 1):
        src = m.group(1)
        d = _mermaid_dir()
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        h = hashlib.sha256(src.encode()).hexdigest()[:16]
        mmd, png = d / f"{h}.mmd", d / f"{h}.png"
        if not png.exists():
            mmd.write_text(src, encoding="utf-8")
            cmd = [os.environ["AIMOST_MERMAID_BIN"], str(mmd), str(png)] if os.environ.get("AIMOST_MERMAID_BIN") else \
                ["docker", "run", "--rm", "--memory", "1g", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{d}:/data",
                 "minlag/mermaid-cli", "-i", f"/data/{h}.mmd", "-o", f"/data/{h}.png", "-b", "white", "-s", "2"]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                err = (r.stderr or r.stdout).strip()[-200:] if r.returncode else ""
            except (OSError, subprocess.TimeoutExpired) as x:
                err = str(x)[:200]
            if err or not png.exists():
                warn(f"diagram {n} not rendered — left as code: {err}")
                continue
        pngs.append(str(png))
    return pngs


class Poster:
    """A role's handle on one connection: its bot (or the relay), API calls, channel resolution, text checks."""

    def __init__(self, role: str, via: Optional[str] = None, warn: Callable[[str], None] = None):
        self.role = role
        self.warn = warn or (lambda m: print(f"aimost post: {m}", file=sys.stderr))
        if via:
            conn = connections.connections().get(via)
            if not conn:
                raise PostError(f"no connection {via!r} in {paths.config_dir() / 'connections'}")
            if not connections.admitted(conn, role):
                raise PostError(f"session {role} is not admitted by connection {via} — nothing posted", 3)
        else:
            conn = connections.own_connection(role)
            if not conn:
                raise PostError(f"session {role} is not admitted by any connection — nothing posted", 3)
        self.conn = conn
        self.relay: Optional[str] = None
        self.token = connections.bot(conn, role)
        if not self.token:
            self.token = connections.relay(conn, role)
            if self.token:
                self.relay = conn["RELAY"].strip()
        if not self.token:
            raise PostError(f"{role} has no bot in connection {conn['NAME']} (and no relay) — nothing posted", 3)
        self._me: Optional[dict] = None

    # ── basics ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def api(self, method: str, path: str, body: object = None):
        return mattermost.api(self.conn, method, path, body, token=self.token)

    @property
    def me(self) -> dict:
        if self._me is None:
            self._me = self.api("GET", "/users/me")
        return self._me

    @property
    def sig(self) -> str:
        return f"from session {self.role} · via relay {self.me.get('username', self.relay)}" if self.relay else ""

    def sign(self, t: str) -> str:
        """The relay's signature as the first line (already there — not repeated)."""
        if not self.sig or (t or "").startswith(self.sig):
            return t
        return self.sig + ("\n" + t if t else "")

    def _not_member(self, where: str):
        raise PostError(f"bot @{self.me.get('username', self.role)} is not in {where} — add it to the channel (--invite "
                        "does not help: it adds addressees and mentioned people) or post where it is", 8)

    def _as_member(self, where, f):
        try:
            return f()
        except urllib.error.HTTPError as x:
            if x.code != 403:
                raise
            self._not_member(where() if callable(where) else where)

    def channel_name(self, cid: str) -> str:
        try:
            return self.api("GET", f"/channels/{cid}").get("name") or cid
        except Exception:
            return cid

    def dm_with(self, login: str) -> str:
        try:
            uid = self.api("GET", f"/users/username/{login}")["id"]
        except urllib.error.HTTPError as x:
            if x.code != 404:
                raise
            raise PostError(f"no user @{login} in this chat — `aimost who <name>`", 6)
        return self.api("POST", "/channels/direct", [self.me["id"], uid])["id"]

    def check(self, draft: textcheck.Draft) -> textcheck.Draft:
        draft.conn, draft.call, draft.me = self.conn, self.api, self.me["id"]
        try:
            textcheck.run(draft)
        except textcheck.Refused as r:
            raise PostError(r.hint, r.code)
        finally:
            for n in draft.notes:
                self.warn(n)
            draft.notes = []
        return draft

    # ── where ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def resolve(self, channel: str = "dm", thread: str = "", new_thread: bool = False) -> Dict[str, object]:
        """→ {channel_id, thread, auto_thread}. Applies the turn thread and the relay rules."""
        channel = channel or "dm"
        auto = False
        if not thread and not new_thread and channel == "dm":
            turn = turn_state(self.role)
            if turn and not turn.get("ended") and turn.get("via") == self.conn["NAME"] and turn.get("root"):
                thread, auto = turn["root"], True
            if auto and self.relay:          # the turn's thread may belong to another bot: the relay then starts anew
                try:
                    self.api("GET", f"/posts/{thread}")
                except Exception:
                    thread, auto = "", False
        if self.relay and channel != "dm":
            raise PostError(f"relay {self.relay} posts only into the direct message of "
                            f"{self.conn.get('OWNER', 'the owner')} (`--channel dm`, no --pin): channels and other people "
                            f"— with the session's own bot (`aimost bot add {self.role}` on the owner's word) — nothing "
                            "posted", 3)
        if thread and channel == "dm":
            # a thread knows its channel; `dm` from an event is the DM with the event's author, not with the owner
            cid = self._as_member(f"the channel of thread {thread} (closed to the bot)",
                                  lambda: self.api("GET", f"/posts/{thread}"))["channel_id"]
        elif channel == "dm" or channel.startswith("dm:"):
            peer = channel[3:].lstrip("@") if channel.startswith("dm:") else self.conn.get("OWNER", "")
            if not peer:
                raise PostError(f"connection {self.conn['NAME']} has no OWNER — say where: --channel <name>|dm:<login>")
            cid = self.dm_with(peer)
        else:
            try:
                cid = self._as_member(channel, lambda: self.api(
                    "GET", f"/teams/name/{self.conn.get('TEAM', '')}/channels/name/{channel}"))["id"]
            except urllib.error.HTTPError as x:
                if x.code != 404:
                    raise
                raise PostError(f"no channel {channel!r} in team {self.conn.get('TEAM', '')}")
        if self.relay:
            dm = self.dm_with(self.conn["OWNER"])
            if thread:
                r = self.api("GET", f"/posts/{thread}")
                r = self.api("GET", f"/posts/{r['root_id']}") if r.get("root_id") else r
                if r.get("channel_id") != dm or r.get("user_id") != self.me["id"] or \
                        (r.get("props") or {}).get("agent") != self.role:
                    if not auto:
                        raise PostError(f"thread {thread} is not a thread of session {self.role} in the DM of relay "
                                        f"{self.relay}: the relay posts only into its own threads of the owner's DM — "
                                        "nothing posted", 3)
                    thread, auto = "", False
                else:
                    thread = r["id"]
            cid = dm
        return {"channel_id": cid, "thread": thread, "auto_thread": auto}

    def _props(self) -> dict:
        return {"agent": self.role, **({"relay": self.relay} if self.relay else {})}

    def _own(self, post: dict) -> bool:
        return post.get("user_id") == self.me["id"] and (not self.relay or (post.get("props") or {}).get("agent") == self.role)

    def _turn_author(self, post: dict) -> Optional[str]:
        """The author of the message that started the chat turn, when this reply goes into that turn's thread in a channel:
        people get thread notifications only when mentioned. Not for peers, an author already mentioned, DMs."""
        t = turn_state(self.role) or {}
        u = (t.get("from") or "").lstrip("@")
        if t.get("ended") or t.get("via") != self.conn["NAME"] or not u or t.get("role") not in ("owner", "person") \
                or post.get("root_id") != t.get("root") or u.lower() == (self.me.get("username") or "").lower():
            return None
        if textcheck.mentioned(post.get("message") or "", u):
            return None
        try:
            if self.api("GET", f"/channels/{post['channel_id']}").get("type") in ("D", "G"):
                return None
        except Exception:
            return None
        return u

    def _phone_copy(self, channel_id: str, root: str, text: str) -> None:
        pngs = mermaid_images(text, self.warn)
        if pngs:
            self.api("POST", "/posts", {"channel_id": channel_id, "root_id": root, "props": self._props(),
                                        "message": self.sign("🖼 diagrams as images — for phones (desktop draws them in "
                                                             "the post)"),
                                        "file_ids": [mattermost.upload(self.conn, channel_id, f, self.token)
                                                     for f in pngs[:10]]})

    # ── a post ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def send(self, text: str = "", *, files: List[str] = (), channel: str = "dm", thread: str = "",
             new_thread: bool = False, attach: bool = False, invite: bool = False, plain: bool = False,
             pin: str = "") -> List[str]:
        """A text post (and/or files). ``.md`` without text — the post's text, in parts into the thread when long. → lines
        of output."""
        files = list(files)
        for f in files:
            if not os.path.isfile(f):
                raise PostError(f"no file {f}")
        if not text and not files:
            raise PostError("nothing to post: text or --file")
        if pin and thread:
            raise PostError("--pin — a top-level post with text, no --thread and no buttons")
        where = self.resolve(channel, "" if pin else thread, new_thread or bool(pin))
        cid = where["channel_id"]
        md_text = not text and len(files) == 1 and files[0].endswith(".md")
        chunks = [text]
        if md_text:
            with open(files[0], encoding="utf-8", errors="replace") as f:
                chunks = textcheck.md_chunks(f.read())
        d = self.check(textcheck.Draft(chunks[0], chunks=chunks[1:], files=files, plain=plain, channel_id=cid,
                                       invite=invite, layout=not md_text and not pin))
        chunks = [d.message] + d.chunks
        if pin and len(chunks) > 1:
            raise PostError("--pin: the text is longer than one post — shorten it (a pin is one post)")
        post = {"channel_id": cid, "props": self._props(),
                "message": chunks[0] + (f"\n\n_(part 1 of {len(chunks)}, the rest — in the thread)_"
                                        if len(chunks) > 1 else "")}
        if where["thread"]:
            post["root_id"] = where["thread"]
        post["message"] = self.sign(post["message"])
        if post.get("root_id") and post["message"] and not pin:
            au = self._turn_author(post)
            if au:
                post["message"] = f"@{au} " + post["message"]
        if files and not pin and (not md_text or attach):
            post["file_ids"] = [mattermost.upload(self.conn, cid, f, self.token) for f in files]
        if pin:
            return self._pin(post, pin)
        p = self._as_member(lambda: self.channel_name(cid), lambda: self.api("POST", "/posts", post))
        root = post.get("root_id") or p["id"]
        self._phone_copy(cid, root, chunks[0])
        for i, ch in enumerate(chunks[1:], 2):
            self.api("POST", "/posts", {"channel_id": cid, "root_id": root, "props": self._props(),
                                        "message": self.sign(ch) + f"\n\n_(part {i} of {len(chunks)})_"})
            self._phone_copy(cid, root, ch)
        out = [f"posted ({self.conn['NAME']}, post {p['id']}" + (f", {len(chunks)} parts" if len(chunks) > 1 else "") + ")"]
        if where["auto_thread"] and post.get("root_id"):
            self.warn(f"into the turn's thread {post['root_id']} (a new post — --new-thread)")
        return out

    def _pin(self, post: dict, key: str) -> List[str]:
        """One pinned post per key and channel, edited in place."""
        post.pop("root_id", None)
        post["props"][P("pin")] = key
        cid = post["channel_id"]
        pinned = self.api("GET", f"/channels/{cid}/pinned") or {}
        mine = [x for x in (pinned.get("posts") or {}).values()
                if x.get("user_id") == self.me["id"] and (x.get("props") or {}).get(P("pin")) == key]
        if mine:
            old = max(mine, key=lambda x: x["create_at"])
            if old.get("message") == post.get("message"):
                return [f"pin {key!r} unchanged (post {old['id']})"]
            self.api("PUT", f"/posts/{old['id']}/patch", {"message": post["message"]})
            return [f"pin {key!r} updated (post {old['id']})"]
        p = self._as_member(lambda: self.channel_name(cid), lambda: self.api("POST", "/posts", post))
        self.api("POST", f"/posts/{p['id']}/pin")
        return [f"pin {key!r} created (post {p['id']})"]

    # ── a question ─────────────────────────────────────────────────────────────────────────────────────────────────
    def ask(self, spec: dict, *, to: str = "", channel: str = "dm", thread: str = "", new_thread: bool = False,
            invite: bool = False, ttl: Optional[float] = None, blocking: bool = False,
            sink: str = "", ref: str = "") -> List[str]:
        """A question with buttons; its answer comes as an ``answer`` event (or to the answer sink ``sink``, with ``ref`` —
        the question's id on the sink's side)."""
        spec = questions.check(questions.norm_rec(spec))
        if ref and (not sink or len(ref) > 200 or any(c.isspace() for c in ref)):
            raise PostError(f"--ref {ref!r}: one word, with --sink")
        if ttl is not None and not ttl > 0:
            raise PostError(f"--ttl {ttl!r}: hours, a number above zero")
        where = self.resolve(channel, thread, new_thread)
        cid = where["channel_id"]
        ctype = self.api("GET", f"/channels/{cid}").get("type")
        if not to and ctype != "D":
            raise PostError("--ask in a channel: to whom? `--to <login>[,<login>…]` — buttons are accepted only from them "
                            f"(the owner — `--to {self.conn.get('OWNER', '<owner>')}`). Without --to — only in a DM.")
        default = channel[3:].lstrip("@") if channel.startswith("dm:") else self.conn.get("OWNER", "")
        spec["to"] = [x.strip().lstrip("@") for x in (to or default).split(",") if x.strip()]
        if to:
            spec["mention"] = list(spec["to"])
        spec["discuss"] = ctype not in ("D", "G")
        if sink:
            spec["sink"] = sink
        if ref:
            spec["ref"] = ref
        d = self.check(textcheck.Draft("", ask=spec, to=spec["to"], channel_id=cid, invite=invite, layout=True))
        spec = d.ask
        if self.sig:
            spec["context"] = self.sign(spec.get("context") or "")   # in the spec: the edge redraws the post from it
        qid = questions.new_id()
        msg, atts = questions.render(qid, spec, {}, self.role, questions.client_auth(self.conn),
                                     questions.action_url(self.conn))
        post = {"channel_id": cid, "message": msg,
                "props": {**self._props(), "attachments": atts, P("question"): qid, P("ask"): spec}}
        if where["thread"]:
            post["root_id"] = where["thread"]
        p = self._as_member(lambda: self.channel_name(cid), lambda: self.api("POST", "/posts", post))
        root = post.get("root_id") or p["id"]
        askstore.ask_record(self.role, {"q": qid, "post": p["id"], "via": self.conn["NAME"], "thread": root,
                                        "at": int(time.time() * 1000), "title": questions.title(spec), "ttl": ttl,
                                        "to": spec["to"], "blocking": blocking, "state": "open",
                                        **({"sink": sink} if sink else {})})
        if where["auto_thread"] and post.get("root_id"):
            self.warn(f"into the turn's thread {post['root_id']} (a new post — --new-thread)")
        return [f"posted ({self.conn['NAME']}, post {p['id']})",
                f"question {qid} — the answer comes into the inbox as an answer event" +
                (f"; deadline {ttl:g} h — unanswered by then, the bridge cancels it" if ttl else
                 f"; lost its point — `aimost post --cancel {qid} \"<why>\"`")]

    def cancel(self, target: str, why: str) -> List[str]:
        """Cancel the role's own question in place, by ask id or post id."""
        why = " ".join(why.split())
        if not why:
            raise PostError('--cancel <ask-id|post_id> "<why>" — the reason is required')
        rec = askstore.asks(self.role).get(target) if target.startswith("ask-") else None
        if target.startswith("ask-") and not rec:
            raise PostError(f"{target} is not among the questions of session {self.role} — give the question's post_id")
        pid = rec["post"] if rec else target
        try:
            old = self.api("GET", f"/posts/{pid}")
        except urllib.error.HTTPError as x:
            if x.code not in (403, 404):
                raise
            raise PostError(f"post {pid} not found or closed to bot {self.role}")
        if not self._own(old):
            raise PostError(f"post {pid} was not written by bot {self.role}" +
                            (f" (relay {self.relay} posts for many sessions — only this one's question)" if self.relay
                             else "") + " — only your own question can be cancelled", 3)
        q = (old.get("props") or {}).get(P("question")) or ""
        if not q.startswith("ask-") or (rec and q != target):
            raise PostError(f"post {pid} is not an `aimost post --ask` question")
        st = askstore.ask_state(old)
        if st == "cancelled":
            why = (old["props"][P("cancelled")] or {}).get("why", "")
            return [f"question {q} already cancelled ({why})"]
        if st == "answered":
            askstore.ask_record(self.role, {"q": q, "state": "answered"})
            raise PostError(f"question {q} is already fully answered — nothing to cancel")
        why = self.check(textcheck.Draft(why, plain=True)).message
        patch = questions.cancelled_patch(old, why, by=self.role)
        self.api("PUT", f"/posts/{pid}/patch", patch)
        askstore.ask_record(self.role, {"q": q, "post": pid, "state": "cancelled",
                                        "cancelled_at": patch["props"][P("cancelled")]["at"], "why": why})
        return [f"question {q} cancelled ({self.conn['NAME']}, post {pid}): {why}"]

    def edit(self, post_id: str, text: str, plain: bool = False) -> List[str]:
        """Replace the text of the role's own post; buttons and files stay."""
        if not text.strip():
            raise PostError('--edit <post_id> "<new text>" — text only')
        old = self.api("GET", f"/posts/{post_id}")
        if not self._own(old):
            raise PostError(f"post {post_id} was not written by bot {self.role}" +
                            (f" (relay {self.relay} posts for many sessions — only this one's post)" if self.relay
                             else "") + " — only your own post can be edited", 3)
        d = self.check(textcheck.Draft(text, plain=plain, channel_id=old.get("channel_id", ""), layout=True))
        self.api("PUT", f"/posts/{post_id}/patch", {"message": self.sign(d.message)})
        return [f"edited ({self.conn['NAME']}, post {post_id})"]

    def conclude(self, dm_root: str, text: str) -> List[str]:
        """Outcome of a question's discussion in a DM: one line into the original thread, a mark into the DM thread."""
        d = askstore.discussions(self.conn["NAME"]).get(dm_root)
        if not d or d.get("asker") != self.role:
            raise PostError(f"thread {dm_root} is not a discussion of a question of session {self.role} (it is opened by "
                            f"the {questions.DISCUSS!r} button)", 3)
        text = self.check(textcheck.Draft(" ".join(text.split()), channel_id=d["channel"])).message
        src = self.api("POST", "/posts", {"channel_id": d["channel"], "root_id": d["thread"], "props": self._props(),
                                          "message": f"💬 Outcome of the discussion with @{d['user']}: {text}"})
        base = self.conn.get("URL", "").rstrip("/") + "/" + self.conn.get("TEAM", "") + "/pl/"
        self.api("POST", "/posts", {"channel_id": d["dm_channel"], "root_id": dm_root, "props": self._props(),
                                    "message": f"✅ outcome sent to the original thread — [post]({base}{src['id']})"})
        return [f"discussion outcome posted ({self.conn['NAME']}, post {src['id']})"]


def open_asks(role: str) -> List[str]:
    """The role's open questions, checked against the server: answered, cancelled or deleted ones leave the list for
    good (recorded in the registry)."""
    conns = connections.connections()
    out = []
    for q, r in askstore.asks(role).items():
        if r.get("state", "open") != "open":
            continue
        c = conns.get(r.get("via") or "")
        tok = (connections.bot(c, role) or connections.relay(c, role)) if c else None
        note = ""
        if c and tok:
            try:
                live = mattermost.api(c, "GET", f"/posts/{r['post']}", token=tok)
                st = "gone" if live.get("delete_at") else askstore.ask_state(live)
            except urllib.error.HTTPError as x:
                st = "gone" if x.code in (403, 404) else "open"
                note = "" if st == "gone" else " (not checked)"
            except Exception:
                st, note = "open", " (not checked)"
            if st != "open":
                askstore.ask_record(role, {"q": q, "state": st})
                continue
        when = time.strftime("%d.%m %H:%M", time.localtime((r.get("at") or 0) / 1000))
        due = f" · deadline {r['ttl']:g} h" if r.get("ttl") else ""
        out.append(f"{q}  {when}  {r.get('via')}  thread {r.get('thread')}  {r.get('title', '')}{due}{note}")
    out.append(f"open questions: {len(out)}" +
               (' — cancel what is stale: aimost post --cancel <ask-id> "<why>"' if out else ""))
    return out
