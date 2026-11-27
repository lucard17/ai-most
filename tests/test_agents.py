"""Agent directory from bot profiles (status spec): offline table rules, publishing against the real server."""

from __future__ import annotations

import os
import uuid

from helpers import IsolatedTest
from itenv import IT, requires_mattermost

from lmost import agents
from lmost import mattermost as mm


class Table(IsolatedTest):
    def test_owner_by_longest_prefix(self):
        conn = {"BOT_OWNERS": "ann:ann, ann-x:bob"}
        self.assertEqual(agents.owner_of("ann-deploy", conn), "@ann")
        self.assertEqual(agents.owner_of("ann-x-review", conn), "@bob")
        self.assertEqual(agents.owner_of("annex", conn), agents.SHARED)
        self.assertEqual(agents.owner_of("ops", {}), agents.SHARED)

    def test_rows_sorted_shared_first_service_apart(self):
        conn = {"BOT_OWNERS": "ann:ann", "BOT_SERVICE": "buttons"}
        users = [{"username": "ann-b", "bot_description": "does b"},
                 {"username": "zeta", "bot_description": "shared |pipe|\n  spaced"},
                 {"username": "ann-a", "bot_description": ""},
                 {"username": "buttons", "bot_description": "x"}]
        t = agents.table(conn, users)
        rows = [r for r in t.splitlines() if r.startswith("| @")]
        self.assertEqual(rows, ["| @zeta | shared | shared /pipe/ spaced |",
                                "| @ann-a | @ann | no description |",
                                "| @ann-b | @ann | does b |"])
        self.assertTrue(t.rstrip().endswith("@buttons"))

    def test_long_description_cut(self):
        self.assertEqual(len(agents.clean("x" * 500)), agents.DESC_MAX + 1)


@requires_mattermost
class Publish(IsolatedTest):
    """`lmost agents publish` on the disposable server: pinned once, edited in place, unchanged → no write."""

    def setUp(self):
        super().setUp()
        self.conn = {"NAME": "it", "URL": IT["LMOST_IT_URL"]}
        self.owner = IT["LMOST_IT_TOKEN_OWNER"]
        self.prefix = "t" + uuid.uuid4().hex[:6]
        cfg = self.tmp / "config"
        (cfg / "connections").mkdir(parents=True)
        (cfg / "bots" / "it").mkdir(parents=True)
        (cfg / "connections" / "it.env").write_text(
            f"URL={IT['LMOST_IT_URL']}\nTEAM={IT['LMOST_IT_TEAM']}\nOWNER=owner\nBOT_OWNERS={self.prefix}:owner\n")
        (cfg / "bots" / "it" / "deployer").write_text(IT["LMOST_IT_BOT_DEPLOYER"])
        self.channel = "dir-" + uuid.uuid4().hex[:8]
        ch = self.call("POST", "/channels", {"team_id": IT["LMOST_IT_TEAM_ID"], "name": self.channel,
                                             "display_name": self.channel, "type": "O"})
        self.ch_id = ch["id"]
        self.call("POST", f"/channels/{ch['id']}/members", {"user_id": IT["LMOST_IT_BOT_ID_DEPLOYER"]})
        self.bot = self.call("POST", "/bots", {"username": f"{self.prefix}-scout",
                                               "display_name": "scout", "description": "finds things"})
        self.call("POST", f"/teams/{IT['LMOST_IT_TEAM_ID']}/members",
                  {"team_id": IT["LMOST_IT_TEAM_ID"], "user_id": self.bot["user_id"]})

    def tearDown(self):
        try:
            self.call("POST", f"/bots/{self.bot['user_id']}/disable")
        except mm.HTTPError:
            pass
        super().tearDown()

    def call(self, method, path, body=None):
        return mm.api(self.conn, method, path, body, token=self.owner)

    def run_publish(self):
        return self.lmost("agents", "publish", "--via", "it", "--channel", self.channel, env={"LMOST_ROLE": "deployer"})

    def pinned(self):
        return list((self.call("GET", f"/channels/{self.ch_id}/pinned").get("posts") or {}).values())

    def test_created_unchanged_updated(self):
        r = self.run_publish()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("directory created", r.stdout)
        pins = self.pinned()
        self.assertEqual(len(pins), 1)
        self.assertIn(f"| @{self.prefix}-scout | @owner | finds things |", pins[0]["message"])
        first = pins[0]

        # The table lists every bot of the team, and other tests add and disable bots on the shared server meanwhile:
        # a run may legitimately say "updated". Repeat until a quiet moment; the quiet run must not write.
        for _ in range(5):
            before = self.call("GET", f"/posts/{first['id']}")["update_at"]
            r = self.run_publish()
            if "directory unchanged" in r.stdout:
                break
            self.assertIn(f"directory updated (post {first['id']})", r.stdout)
        self.assertIn("directory unchanged", r.stdout)
        self.assertEqual(self.call("GET", f"/posts/{first['id']}")["update_at"], before)

        self.call("PUT", f"/bots/{self.bot['user_id']}", {"username": f"{self.prefix}-scout",
                                                          "display_name": "scout", "description": "finds more"})
        r = self.run_publish()
        self.assertIn(f"directory updated (post {first['id']})", r.stdout)
        pins = self.pinned()
        self.assertEqual([p["id"] for p in pins], [first["id"]])
        self.assertIn("finds more", pins[0]["message"])
        self.assertTrue(pins[0]["props"].get(agents.MARK), "an edit keeps the mark, so the next run finds the post")

    def test_disabled_bot_left_out(self):
        self.call("POST", f"/bots/{self.bot['user_id']}/disable")
        r = self.lmost("agents", "table", "--via", "it", env={"LMOST_ROLE": "deployer"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(f"{self.prefix}-scout", r.stdout)
        self.assertIn("@deployer", r.stdout)

    def test_private_guard_blocks(self):
        env = self.tmp / "config" / "connections" / "it.env"
        env.write_text(env.read_text() + "PRIVATE_GUARD=on\n")
        r = self.run_publish()
        self.assertEqual(r.returncode, 5)
        self.assertIn("no private-patterns", r.stderr)
        (self.tmp / "config" / "private-patterns").write_text("# comment\nfinds\\s+thing\n")
        r = self.run_publish()
        self.assertEqual(r.returncode, 5)
        self.assertIn("finds\\s+thing", r.stderr)
        self.assertEqual(self.pinned(), [])

    def test_session_without_bot(self):
        r = self.lmost("agents", "table", "--via", "it", env={"LMOST_ROLE": "nobody"})
        self.assertEqual(r.returncode, 3)
