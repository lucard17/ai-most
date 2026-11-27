"""Mattermost transport and websocket — against the disposable real server (skipped without it); ``describe`` — offline."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import uuid

import helpers  # noqa: F401  (sys.path)
from itenv import IT, requires_mattermost

from lmost import mattermost as mm
from lmost import ws


class Describe(unittest.TestCase):
    def test_files_and_forward(self):
        post = {"metadata": {
            "files": [{"name": "a.png", "id": "f1", "size": 4096}],
            "embeds": [{"type": "permalink", "data": {"channel_display_name": "ops", "post": {
                "user_id": "u1", "message": "original", "metadata": {"files": [{"name": "b.txt", "id": "f2"}]}}}}]}}
        out = mm.describe(post, lambda m, p: {"username": "ann"})
        self.assertIn("📎 files (lmost file <id>): a.png (f1, 4 KB)", out)
        self.assertIn("↪ forwarded (@ann, ops): original", out)
        self.assertIn("in the forwarded post: b.txt (f2)", out)
        self.assertEqual(mm.describe({}), "")


@requires_mattermost
class RealServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = {"NAME": "it", "URL": IT["LMOST_IT_URL"]}
        cls.bot = IT["LMOST_IT_BOT_DEPLOYER"]
        cls.owner_tok = IT["LMOST_IT_TOKEN_OWNER"]
        cls.bot_id = mm.api(cls.conn, "GET", "/users/me", token=cls.bot)["id"]
        cls.owner_id = mm.api(cls.conn, "GET", "/users/me", token=cls.owner_tok)["id"]
        cls.dm = mm.api(cls.conn, "POST", "/channels/direct", [cls.bot_id, cls.owner_id], token=cls.bot)["id"]

    def call(self, method, path, body=None, token=None):
        return mm.api(self.conn, method, path, body, token=token or self.bot)

    def test_me_is_the_bot(self):
        me = self.call("GET", "/users/me")
        self.assertEqual(me["username"], "deployer")
        self.assertTrue(me.get("is_bot"))

    def test_bad_token_is_http_401(self):
        with self.assertRaises(mm.HTTPError) as e:
            self.call("GET", "/users/me", token="nope")
        self.assertEqual(e.exception.code, 401)

    def test_post_patch_replaces_props_whole(self):
        """The bridge copies all props before a patch (cancel, answers) because Mattermost replaces them whole."""
        p = self.call("POST", "/posts", {"channel_id": self.dm, "message": "q", "props": {"a": 1, "keep": "x"}})
        self.call("PUT", f"/posts/{p['id']}/patch", {"props": {"b": 2}})
        props = self.call("GET", f"/posts/{p['id']}")["props"]
        self.assertEqual(props.get("b"), 2)
        self.assertNotIn("a", props)
        self.assertNotIn("keep", props)

    def test_thread_and_reaction(self):
        root = self.call("POST", "/posts", {"channel_id": self.dm, "message": "root"})
        self.call("POST", "/posts", {"channel_id": self.dm, "message": "reply", "root_id": root["id"]},
                  token=self.owner_tok)
        th = self.call("GET", f"/posts/{root['id']}/thread")
        self.assertEqual(len(th["order"]), 2)
        self.call("POST", "/reactions", {"user_id": self.bot_id, "post_id": root["id"], "emoji_name": "eyes"})
        got = self.call("GET", f"/posts/{root['id']}")
        self.assertEqual([r["emoji_name"] for r in got["metadata"]["reactions"]], ["eyes"])

    def test_upload_download_describe(self):
        body = uuid.uuid4().bytes * 100
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(body)
        try:
            fid = mm.upload(self.conn, self.dm, f.name, self.bot)
        finally:
            os.unlink(f.name)
        p = self.call("POST", "/posts", {"channel_id": self.dm, "message": "file", "file_ids": [fid]})
        self.assertEqual(mm.download(self.conn, fid, self.bot), body)
        self.assertIn(f"({fid}, 1 KB)", mm.describe(self.call("GET", f"/posts/{p['id']}")))

    def test_websocket_wakes_on_a_post(self):
        events, seen = [], threading.Event()

        def on_event(name):
            events.append(name)
            if name == "posted":
                seen.set()

        w = ws.Watcher(self.conn["URL"], self.bot, on_event)
        w.start()
        try:
            deadline = time.time() + 10
            while "hello" not in events and time.time() < deadline:
                time.sleep(0.05)
            self.assertIn("hello", events, "a pass right after connecting")
            self.call("POST", "/posts", {"channel_id": self.dm, "message": "ping the bot"}, token=self.owner_tok)
            self.assertTrue(seen.wait(10), f"no 'posted' event, got {events}")
        finally:
            w.stop()


if __name__ == "__main__":
    unittest.main()
