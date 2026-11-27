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

from lmost import buttons, commands, heartbeat
from lmost import mattermost as mm

requires_edge = unittest.skipUnless(IT.get("LMOST_IT_EDGE_URL"), "no edge in the test world (tests/integration/edge-up.sh)")


@requires_mattermost
@requires_edge
class RealEdge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = {"NAME": "it", "URL": IT["LMOST_IT_URL"]}
        cls.owner = IT["LMOST_IT_TOKEN_OWNER"]
        cls.colleague = IT["LMOST_IT_TOKEN_COLLEAGUE"]
        cls.bot = IT["LMOST_IT_BOT_DEPLOYER"]
        cls.bot_id = IT["LMOST_IT_BOT_ID_DEPLOYER"]
        cls.public = IT["LMOST_IT_EDGE_PUBLIC"]
        cls.secret = IT["LMOST_IT_EDGE_SECRET"]
        cls.slash_secret = IT["LMOST_IT_EDGE_SLASH_SECRET"]
        ch = mm.api(cls.conn, "POST", "/channels", {"team_id": IT["LMOST_IT_TEAM_ID"], "name": "edge-" + uuid.uuid4().hex[:10],
                                                    "display_name": "edge test", "type": "O"}, token=cls.owner)
        cls.channel = ch["id"]
        for uid in (IT["LMOST_IT_USER_ID_COLLEAGUE"], cls.bot_id):
            mm.api(cls.conn, "POST", f"/channels/{cls.channel}/members", {"user_id": uid}, token=cls.owner)

    def call(self, method, path, body=None, token=None):
        return mm.api(self.conn, method, path, body, token=token or self.bot)

    def ask(self, spec, auth):
        msg, atts = buttons.render("ask-" + uuid.uuid4().hex[:8], spec, {}, "deployer", auth, self.public)
        q = atts[0]["actions"][0]["integration"]["context"]["q"]
        p = self.call("POST", "/posts", {"channel_id": self.channel, "message": msg,
                                         "props": {"attachments": atts, "lmost_question": q, "lmost_ask": spec,
                                                   "agent": "deployer"}})
        return self.call("GET", f"/posts/{p['id']}")

    def press(self, post, part, option, token):
        aid = f"p{part}o{option}"            # the server keeps action ids and hides the integration from clients
        return self.call("POST", f"/posts/{post['id']}/actions/{aid}", {}, token=token)

    def edge_slash(self, name, user_id, text):
        body = urllib.parse.urlencode({"user_id": user_id, "user_name": "?", "team_id": IT["LMOST_IT_TEAM_ID"],
                                       "text": text}).encode()
        url = f"{IT['LMOST_IT_EDGE_URL']}/{name}?k={commands.slash_key(self.slash_secret, name)}"
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=10) as r:
            return json.loads(r.read())["text"]

    def test_a_person_presses_a_button(self):
        spec = {"context": "deploy?", "to": ["owner"],
                "parts": [{"title": "now?", "text": "", "options": ["yes", "no"]},
                          {"title": "notify?", "text": "", "options": ["yes", "no"]}]}
        post = self.ask(spec, {"secret": self.secret})
        self.press(post, 1, 2, self.owner)
        got = self.call("GET", f"/posts/{post['id']}")
        a = got["props"]["lmost_answers"]["1"]
        self.assertEqual((a["option"], a["by"], a["user_id"]), (2, "owner", IT["LMOST_IT_USER_ID_OWNER"]))
        self.assertEqual(got["props"]["lmost_ask"], spec, "the spec survives the redraw")
        self.assertIn("Answered 1 of 2", got["message"])
        # not the addressee: the edge answers ephemerally, the post does not change
        self.press(got, 2, 1, self.colleague)
        again = self.call("GET", f"/posts/{post['id']}")
        self.assertNotIn("2", again["props"]["lmost_answers"])

    def test_owner_key_signs_buttons(self):
        out = self.edge_slash("bot-token", IT["LMOST_IT_USER_ID_OWNER"], "key")
        kv = dict(ln.split("=", 1) for ln in out.splitlines() if ln.startswith("EDGE_"))
        try:
            spec = {"context": "", "parts": [{"title": "go?", "text": "", "options": ["yes"]}]}
            post = self.ask(spec, {"secret": kv["EDGE_SECRET"], "kid": kv["EDGE_KEY_ID"]})
            self.press(post, 1, 1, self.owner)
            self.assertEqual(self.call("GET", f"/posts/{post['id']}")["props"]["lmost_answers"]["1"]["option"], 1)
            forged = self.ask(spec, {"secret": "0" * 64, "kid": kv["EDGE_KEY_ID"]})
            with self.assertRaises(mm.HTTPError):
                self.press(forged, 1, 1, self.owner)          # the edge answers 403, Mattermost reports the failure
            self.assertNotIn("lmost_answers", self.call("GET", f"/posts/{forged['id']}")["props"])
        finally:
            self.assertIn("revoked", self.edge_slash("bot-token", IT["LMOST_IT_USER_ID_OWNER"], "key revoke"))

    def register(self):
        env = dict(os.environ, PYTHONPATH=helpers.SRC, LMOST_MM_TOKEN=self.owner, LMOST_MM_URL=IT["LMOST_IT_URL"],
                   EDGE_SLASH_SECRET=self.slash_secret, EDGE_PUBLIC=self.public)
        r = subprocess.run([sys.executable, "-m", "lmost", "commands", "register", "--team", IT["LMOST_IT_TEAM"], "--create"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def execute(self, command, token, **extra):
        return self.call("POST", "/commands/execute", {"channel_id": self.channel, "team_id": IT["LMOST_IT_TEAM_ID"],
                                                       "command": command, **extra}, token=token)

    def test_slash_commands_through_mattermost(self):
        self.register()
        self.register()                                       # idempotent: updates, no duplicates
        cmds = self.call("GET", f"/commands?team_id={IT['LMOST_IT_TEAM_ID']}&custom_only=true", token=self.owner)
        self.assertEqual(sorted(c["trigger"] for c in cmds if not c.get("delete_at")), ["agent", "bot-token", "unfollow"])
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

    def test_session_command_becomes_a_signed_post(self):
        self.register()
        heartbeat.write(self.conn, self.bot, "deployer", "idle")
        root = self.call("POST", "/posts", {"channel_id": self.channel, "message": "thread root"})
        r = self.execute('/agent ask deployer "status of the release?"', self.colleague, root_id=root["id"],
                         parent_id=root["id"])
        self.assertEqual(r.get("text"), "Sent to @deployer.", r)
        dm = self.call("POST", "/channels/direct", [IT["LMOST_IT_USER_ID_COLLEAGUE"], self.bot_id])
        posts = self.call("GET", f"/channels/{dm['id']}/posts?per_page=5")
        cmd_posts = [posts["posts"][i] for i in posts["order"] if "lmost_command" in posts["posts"][i].get("props", {})]
        self.assertTrue(cmd_posts, "a command post in the colleague's DM with the bot")
        p = cmd_posts[0]
        c = p["props"]["lmost_command"]
        self.assertEqual(p["user_id"], IT["LMOST_IT_EDGE_BOT_ID"], "written by the edge's bot")
        self.assertEqual((c["sub"], c["args"], c["caller"]["id"]),
                         ("ask", ["deployer", "status of the release?"], IT["LMOST_IT_USER_ID_COLLEAGUE"]))
        self.assertTrue(commands.verify(c, self.secret), "signed with the owner's (shared) key")
        self.assertEqual(c["root_id"], root["id"], "the thread the command was called in")


if __name__ == "__main__":
    unittest.main()
