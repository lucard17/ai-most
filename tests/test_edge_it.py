"""The edge against the disposable real Mattermost (edge and commands specs): a person presses a real button, Mattermost
calls the edge, the post's answers change; slash commands registered and executed by Mattermost itself.

Needs ``tests/integration/up.sh`` (which runs ``edge-up.sh``); skipped otherwise.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
import urllib.parse
import urllib.request
import uuid
import json

import helpers  # noqa: F401  (sys.path)
from itenv import IT, requires_mattermost

from aimost import commands, heartbeat, questions
from aimost import mattermost as mm

requires_edge = unittest.skipUnless(IT.get("AIMOST_IT_EDGE_URL"), "no edge in the test world (tests/integration/edge-up.sh)")


@requires_mattermost
@requires_edge
class RealEdge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = {"NAME": "it", "URL": IT["AIMOST_IT_URL"]}
        cls.owner = IT["AIMOST_IT_TOKEN_OWNER"]
        cls.colleague = IT["AIMOST_IT_TOKEN_COLLEAGUE"]
        cls.bot = IT["AIMOST_IT_BOT_DEPLOYER"]
        cls.bot_id = IT["AIMOST_IT_BOT_ID_DEPLOYER"]
        cls.public = IT["AIMOST_IT_EDGE_PUBLIC"]
        cls.secret = IT["AIMOST_IT_EDGE_SECRET"]
        cls.slash_secret = IT["AIMOST_IT_EDGE_SLASH_SECRET"]
        ch = mm.api(cls.conn, "POST", "/channels", {"team_id": IT["AIMOST_IT_TEAM_ID"], "name": "edge-" + uuid.uuid4().hex[:10],
                                                    "display_name": "edge test", "type": "O"}, token=cls.owner)
        cls.channel = ch["id"]
        for uid in (IT["AIMOST_IT_USER_ID_COLLEAGUE"], cls.bot_id):
            mm.api(cls.conn, "POST", f"/channels/{cls.channel}/members", {"user_id": uid}, token=cls.owner)

    def call(self, method, path, body=None, token=None):
        return mm.api(self.conn, method, path, body, token=token or self.bot)

    def ask(self, spec, auth):
        url = questions.action_url({"EDGE_PUBLIC": self.public})
        msg, atts = questions.render("ask-" + uuid.uuid4().hex[:8], spec, {}, "deployer", auth, url)
        q = atts[0]["actions"][0]["integration"]["context"]["q"]
        p = self.call("POST", "/posts", {"channel_id": self.channel, "message": msg,
                                         "props": {"attachments": atts, "aimost_question": q, "aimost_ask": spec,
                                                   "agent": "deployer"}})
        return self.call("GET", f"/posts/{p['id']}")

    def press(self, post, part, option, token):
        aid = f"p{part}o{option}"            # the server keeps action ids and hides the integration from clients
        return self.call("POST", f"/posts/{post['id']}/actions/{aid}", {}, token=token)

    def edge_slash(self, name, user_id, text):
        body = urllib.parse.urlencode({"user_id": user_id, "user_name": "?", "team_id": IT["AIMOST_IT_TEAM_ID"],
                                       "text": text}).encode()
        url = f"{IT['AIMOST_IT_EDGE_URL']}/{name}?k={commands.slash_key(self.slash_secret, name)}"
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=10) as r:
            return json.loads(r.read())["text"]

    def test_a_person_presses_a_button(self):
        spec = {"context": "deploy?", "to": ["owner"],
                "parts": [{"title": "now?", "text": "", "options": ["yes", "no"]},
                          {"title": "notify?", "text": "", "options": ["yes", "no"]}]}
        post = self.ask(spec, {"secret": self.secret})
        self.press(post, 1, 2, self.owner)
        got = self.call("GET", f"/posts/{post['id']}")
        a = got["props"]["aimost_answers"]["1"]
        self.assertEqual((a["option"], a["by"], a["user_id"]), (2, "owner", IT["AIMOST_IT_USER_ID_OWNER"]))
        self.assertEqual(got["props"]["aimost_ask"], spec, "the spec survives the redraw")
        self.assertIn("Answered 1 of 2", got["message"])
        # not the addressee: the edge answers ephemerally, the post does not change
        self.press(got, 2, 1, self.colleague)
        again = self.call("GET", f"/posts/{post['id']}")
        self.assertNotIn("2", again["props"]["aimost_answers"])

    def test_owner_key_signs_buttons(self):
        out = self.edge_slash("bot-token", IT["AIMOST_IT_USER_ID_OWNER"], "key")
        kv = dict(ln.split("=", 1) for ln in out.splitlines() if ln.startswith("EDGE_"))
        try:
            spec = {"context": "", "parts": [{"title": "go?", "text": "", "options": ["yes"]}]}
            post = self.ask(spec, {"secret": kv["EDGE_SECRET"], "kid": kv["EDGE_KEY_ID"]})
            self.press(post, 1, 1, self.owner)
            self.assertEqual(self.call("GET", f"/posts/{post['id']}")["props"]["aimost_answers"]["1"]["option"], 1)
            forged = self.ask(spec, {"secret": "0" * 64, "kid": kv["EDGE_KEY_ID"]})
            with self.assertRaises(mm.HTTPError):
                self.press(forged, 1, 1, self.owner)          # the edge answers 403, Mattermost reports the failure
            self.assertNotIn("aimost_answers", self.call("GET", f"/posts/{forged['id']}")["props"])
        finally:
            self.assertIn("revoked", self.edge_slash("bot-token", IT["AIMOST_IT_USER_ID_OWNER"], "key revoke"))

    def register(self):
        env = dict(os.environ, PYTHONPATH=helpers.SRC, AIMOST_MM_TOKEN=self.owner, AIMOST_MM_URL=IT["AIMOST_IT_URL"],
                   EDGE_SLASH_SECRET=self.slash_secret, EDGE_PUBLIC=self.public, AIMOST_PLUGINS="health")
        r = subprocess.run([sys.executable, "-m", "aimost", "commands", "register", "--team", IT["AIMOST_IT_TEAM"], "--create"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def execute(self, command, token, **extra):
        return self.call("POST", "/commands/execute", {"channel_id": self.channel, "team_id": IT["AIMOST_IT_TEAM_ID"],
                                                       "command": command, **extra}, token=token)

    def test_slash_commands_through_mattermost(self):
        self.register()
        self.register()                                       # idempotent: updates, no duplicates
        cmds = self.call("GET", f"/commands?team_id={IT['AIMOST_IT_TEAM_ID']}&custom_only=true", token=self.owner)
        self.assertEqual(sorted(c["trigger"] for c in cmds if not c.get("delete_at")), ["agent", "bot-token", "health", "unfollow"])
        heartbeat.write(self.conn, self.bot, "deployer", "running", unread=3, last_turn=int(time.time() * 1000))
        edge_view = heartbeat.fetch(lambda m, p, b=None: self.call(m, p, b, token=self.owner), self.bot_id)
        self.assertEqual(edge_view["unread"], 3, "a bot writes its own heartbeat, an admin reads it")
        with self.assertRaises(mm.HTTPError):
            self.call("GET", f"/users/{self.bot_id}/preferences/{heartbeat.CATEGORY}", token=self.colleague)
        r = self.execute("/agent status deployer", self.owner)
        self.assertIn("@deployer — running · unread 3", r.get("text", ""), r)
        self.assertIn("s old_", r["text"])
        r = self.execute("/agent who", self.colleague)
        self.assertIn("not open to you", r.get("text", ""), r)

    def test_health_through_mattermost(self):
        """/health end to end: Mattermost calls the edge, the edge probes, the delayed answer comes back through the
        command's response_url as an ephemeral post the caller receives over the websocket."""
        from aimost import ws
        targets = {"stand": {"local": {"edge": {"url": "http://127.0.0.1:8767", "path": "/version", "rule": "http"},
                                       "chat": {"url": "http://app:8065", "path": "/api/v4/system/ping", "rule": "status"},
                                       "cache": None},
                             "far": {"api": {"url": "http://app:9", "path": "/health", "rule": "status"}}}}
        d = os.path.join(os.path.dirname(__file__), "integration", "health")
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "targets.json.tmp")
        with open(tmp, "w") as f:
            json.dump(targets, f)
        os.replace(tmp, os.path.join(d, "targets.json"))
        self.register()
        sock = ws.WS(IT["AIMOST_IT_URL"], self.colleague, timeout=1)
        try:
            r = self.execute("/health stand", self.colleague)
            self.assertEqual(r.get("text"), "⏳ checking…", r)
            text, deadline = None, time.time() + 10
            while text is None and time.time() < deadline:
                try:
                    m = sock.recv()
                except Exception:
                    continue
                ev = json.loads(m) if m else {}
                if ev.get("event") == "ephemeral_message":
                    post = json.loads(ev["data"]["post"])
                    if post.get("channel_id") == self.channel and "STAND" in post.get("message", ""):
                        text = post["message"]
        finally:
            sock.close()
        self.assertIsNotNone(text, "no delayed answer over the websocket")
        self.assertEqual(text.split("\n")[0], "🔴 STAND: 1/2 ok · 🔴 1 failed · ⚪ 1 no probe · far — no link to contour")
        self.assertIn("| edge | 🟢 | — |", text)
        self.assertIn("| chat | 🔴 | — |", text, "the ping says OK, not ok: the status rule is exact")
        self.assertIn("| api | — | ❔ |", text)
        self.assertNotIn("8065", text)

    def test_session_command_becomes_a_signed_post(self):
        self.register()
        heartbeat.write(self.conn, self.bot, "deployer", "idle")
        root = self.call("POST", "/posts", {"channel_id": self.channel, "message": "thread root"})
        r = self.execute('/agent ask deployer "status of the release?"', self.colleague, root_id=root["id"],
                         parent_id=root["id"])
        self.assertEqual(r.get("text"), "Sent to @deployer.", r)
        dm = self.call("POST", "/channels/direct", [IT["AIMOST_IT_USER_ID_COLLEAGUE"], self.bot_id])
        posts = self.call("GET", f"/channels/{dm['id']}/posts?per_page=5")
        cmd_posts = [posts["posts"][i] for i in posts["order"] if "aimost_command" in posts["posts"][i].get("props", {})]
        self.assertTrue(cmd_posts, "a command post in the colleague's DM with the bot")
        p = cmd_posts[0]
        c = p["props"]["aimost_command"]
        self.assertEqual(p["user_id"], IT["AIMOST_IT_EDGE_BOT_ID"], "written by the edge's bot")
        self.assertEqual((c["sub"], c["args"], c["caller"]["id"]),
                         ("ask", ["deployer", "status of the release?"], IT["AIMOST_IT_USER_ID_COLLEAGUE"]))
        self.assertTrue(commands.verify(c, self.secret), "signed with the owner's (shared) key")
        self.assertEqual(c["root_id"], root["id"], "the thread the command was called in")


if __name__ == "__main__":
    unittest.main()


@requires_mattermost
@requires_edge
class ConfirmThroughEdge(helpers.IsolatedTest):
    """`aimost confirm` posts ✅/❌, the owner presses ✅ in Mattermost, the edge records it, the waiting call returns."""

    def test_owner_confirms(self):
        import threading
        from aimost import confirm
        cfg = self.tmp / "config"
        (cfg / "connections").mkdir(parents=True)
        (cfg / "bots" / "it").mkdir(parents=True)
        (cfg / "connections" / "it.env").write_text(
            f"URL={IT['AIMOST_IT_URL']}\nTEAM={IT['AIMOST_IT_TEAM']}\nOWNER=owner\nSESSIONS=deployer\n"
            f"EDGE_PUBLIC={IT['AIMOST_IT_EDGE_PUBLIC']}\nEDGE_SECRET={IT['AIMOST_IT_EDGE_SECRET']}\n")
        (cfg / "bots" / "it" / "deployer").write_text(IT["AIMOST_IT_BOT_DEPLOYER"])
        out = {}

        def run():
            try:
                out["id"] = confirm.request("deployer", "restart api " + uuid.uuid4().hex[:6], via="it", timeout=30)
            except confirm.ConfirmError as e:
                out["err"] = (e.code, str(e))
        t = threading.Thread(target=run)
        t.start()
        conn = {"NAME": "it", "URL": IT["AIMOST_IT_URL"]}
        dm = mm.api(conn, "POST", "/channels/direct", [IT["AIMOST_IT_BOT_ID_DEPLOYER"], IT["AIMOST_IT_USER_ID_OWNER"]],
                    token=IT["AIMOST_IT_TOKEN_OWNER"])["id"]
        post = None
        for _ in range(100):
            r = mm.api(conn, "GET", f"/channels/{dm}/posts?per_page=5", token=IT["AIMOST_IT_TOKEN_OWNER"])
            post = next((r["posts"][i] for i in r["order"] if (r["posts"][i].get("props") or {}).get("aimost_confirm_request")
                         and not r["posts"][i]["props"].get("aimost_confirm")), None)
            if post:
                break
            time.sleep(0.1)
        self.assertIsNotNone(post, "the confirmation post appeared")
        mm.api(conn, "POST", f"/posts/{post['id']}/actions/yes", {}, token=IT["AIMOST_IT_TOKEN_OWNER"])
        t.join(40)
        self.assertEqual(out.get("id"), post["props"]["aimost_confirm_request"], out)
        done = mm.api(conn, "GET", f"/posts/{post['id']}", token=IT["AIMOST_IT_TOKEN_OWNER"])
        self.assertIn("✅ confirmed — @owner", done["message"])
