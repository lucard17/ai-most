"""Activity: chat turns, progress post, reactions, bot status, waiting notice (status spec)."""

from __future__ import annotations

import json
import os
import time
import uuid

from helpers import IsolatedTest
from itenv import IT, requires_mattermost

from lmost import activity
from lmost import mattermost as mm

ENV = ("LMOST_ACTIVITY_SYNC", "LMOST_PROGRESS_AFTER_S", "LMOST_PROGRESS_EVERY_S", "LMOST_ACTIVITY_DIR")


class Offline(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.calls = []
        self._begin = activity.begin
        activity.begin = lambda role, ev: self.calls.append((role, ev))

    def tearDown(self):
        activity.begin = self._begin
        super().tearDown()

    def test_ack_picks_last_person_message_or_answer(self):
        activity.on_ack("r", [{"role": "owner", "via": "c", "post": "p1"},
                              {"role": "person", "via": "c", "kind": "answer", "post": "p2"},
                              {"role": "peer", "via": "c", "post": "p3"},
                              {"role": "owner", "kind": "cmd", "via": "c", "post": "p4"},
                              {"role": "owner", "post": "p5"}])
        self.assertEqual([ev["post"] for _, ev in self.calls], ["p2"])
        activity.on_ack("r", [{"role": "peer", "via": "c", "post": "p3"}])
        self.assertEqual(len(self.calls), 1)

    def test_states_waiting_wins(self):
        state, why = activity._states([{"name": "a", "status": "busy"}, {"name": "a", "status": "waiting",
                                                                         "waitingFor": "permission"},
                                       {"name": "b", "status": None}, {"name": "c", "status": "idle"}])
        self.assertEqual(state, {"a": "waiting", "c": "idle"})
        self.assertEqual(activity.status_of("a", "waiting", why["a"]), ("raised_hand", "waiting for permission"))
        self.assertEqual(activity.status_of("b", "off"), ("black_circle", "not running"))
        self.assertEqual(activity.status_of("c", "idle"), ("", ""))
        self.assertEqual(activity.status_of("d", "busy"), ("hammer", "working"))

    def test_render(self):
        st = {"since": time.time() - 75, "n": 3, "steps": ["a", "b"], "show_steps": True}
        self.assertEqual(activity.render(st).splitlines(), ["⏳ running 1 min 15 s · steps 3", "· a", "· b"])
        st["show_steps"] = False
        self.assertEqual(activity.render(st), "⏳ running 1 min 15 s · steps 3")
        self.assertTrue(activity.render(st, done=True).startswith("✅ in 1 min"))

    def test_last_action_from_transcript(self):
        d = self.tmp / "claude" / "projects" / "x"
        d.mkdir(parents=True)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "claude")
        (d / "s1.jsonl").write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "rm -rf  build", "description": ""}}]}}) + "\n{bad\n")
        self.assertEqual(activity.last_action("s1"), "Bash: rm -rf build")
        self.assertEqual(activity.last_action("nope"), "")


@requires_mattermost
class Real(IsolatedTest):
    """On the disposable server with a fresh bot per test (its status and reactions are not shared)."""

    def setUp(self):
        super().setUp()
        self._env = {k: os.environ.get(k) for k in ENV}
        os.environ.update({"LMOST_ACTIVITY_SYNC": "1", "LMOST_PROGRESS_AFTER_S": "0", "LMOST_PROGRESS_EVERY_S": "0"})
        self.conn = {"NAME": "it", "URL": IT["LMOST_IT_URL"]}
        self.role = "a" + uuid.uuid4().hex[:7]
        b = self.owner("POST", "/bots", {"username": self.role, "display_name": self.role})
        self.bot_id = b["user_id"]
        self.tok = self.owner("POST", f"/users/{self.bot_id}/tokens", {"description": "it"})["token"]
        self.owner("POST", f"/teams/{IT['LMOST_IT_TEAM_ID']}/members",
                   {"team_id": IT["LMOST_IT_TEAM_ID"], "user_id": self.bot_id})
        cfg = self.tmp / "config"
        (cfg / "connections").mkdir(parents=True)
        (cfg / "bots" / "it").mkdir(parents=True)
        self.env_file = cfg / "connections" / "it.env"
        self.env_file.write_text(f"URL={IT['LMOST_IT_URL']}\nTEAM={IT['LMOST_IT_TEAM']}\nOWNER=owner\n"
                                 f"SESSIONS={self.role}\n")
        (cfg / "bots" / "it" / self.role).write_text(self.tok)
        self.dm = self.bot("POST", "/channels/direct", [self.bot_id, IT["LMOST_IT_USER_ID_OWNER"]])["id"]

    def tearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.owner("POST", f"/bots/{self.bot_id}/disable")
        super().tearDown()

    def owner(self, method, path, body=None):
        return mm.api(self.conn, method, path, body, token=IT["LMOST_IT_TOKEN_OWNER"])

    def bot(self, method, path, body=None):
        return mm.api(self.conn, method, path, body, token=self.tok)

    def reactions(self, post):
        return sorted(r["emoji_name"] for r in (self.owner("GET", f"/posts/{post}/reactions") or [])
                      if r["user_id"] == self.bot_id)

    def thread(self, root):
        t = self.owner("GET", f"/posts/{root}/thread")
        return [t["posts"][i] for i in t["order"] if i != root]

    def test_turn_in_a_direct_message(self):
        p = self.owner("POST", "/posts", {"channel_id": self.dm, "message": "please deploy"})
        activity.begin(self.role, {"via": "it", "post": p["id"], "text": "please deploy", "channel": "dm",
                                   "role": "owner", "from": "owner"})
        self.assertEqual(self.reactions(p["id"]), ["hammer"])
        self.assertEqual(activity.turn(self.role)["root"], p["id"])
        activity.step(self.role, "run   the tests")
        activity.step(self.role, "")
        progress = self.thread(p["id"])
        self.assertEqual(len(progress), 1)
        self.assertTrue(progress[0]["props"].get(activity.PROGRESS_PROP))
        self.assertIn("· run the tests", progress[0]["message"])
        activity.tick()
        activity.end(self.role)
        self.assertIsNone(activity.turn(self.role))
        self.assertEqual(self.reactions(p["id"]), ["hammer", "white_check_mark"])
        final = self.thread(p["id"])
        self.assertEqual(len(final), 1)
        self.assertTrue(final[0]["message"].startswith("✅ in"), final[0]["message"])
        self.assertIn("steps 2", final[0]["message"])

    def test_no_progress_post_in_a_channel(self):
        name = "act-" + uuid.uuid4().hex[:8]
        ch = self.owner("POST", "/channels", {"team_id": IT["LMOST_IT_TEAM_ID"], "name": name, "display_name": name,
                                              "type": "O"})
        self.owner("POST", f"/channels/{ch['id']}/members", {"user_id": self.bot_id})
        p = self.owner("POST", "/posts", {"channel_id": ch["id"], "message": "look"})
        activity.begin(self.role, {"via": "it", "post": p["id"], "text": "look", "channel": name, "role": "owner"})
        activity.step(self.role, "read code")
        activity.tick()
        activity.end(self.role)
        self.assertEqual(self.thread(p["id"]), [])
        self.assertEqual(self.reactions(p["id"]), ["hammer", "white_check_mark"])

    def test_progress_off(self):
        self.env_file.write_text(self.env_file.read_text() + "PROGRESS=off\n")
        p = self.owner("POST", "/posts", {"channel_id": self.dm, "message": "x"})
        activity.begin(self.role, {"via": "it", "post": p["id"], "channel": "dm", "role": "owner"})
        activity.step(self.role, "a step")
        activity.end(self.role)
        self.assertEqual(self.thread(p["id"]), [])

    def test_status_sync_writes_only_changes(self):
        p = self.owner("POST", "/posts", {"channel_id": self.dm, "message": "fix   the build please"})
        activity.begin(self.role, {"via": "it", "post": p["id"], "text": "fix   the build please", "channel": "dm",
                                   "role": "owner"})
        activity.status_sync([{"name": self.role, "status": "busy"}])
        cs = json.loads(self.owner("GET", f"/users/{self.bot_id}")["props"]["customStatus"])
        self.assertEqual((cs["emoji"], cs["text"]), ("hammer", "working: fix the build please"))
        activity.status_sync([{"name": self.role, "status": "waiting", "waitingFor": "permission"}])
        cs = json.loads(self.owner("GET", f"/users/{self.bot_id}")["props"]["customStatus"])
        self.assertEqual(cs["text"], "waiting for permission")
        self.assertIn("raised_hand", self.reactions(p["id"]))
        # unchanged → no write: a status set by hand stays
        self.owner("PUT", f"/users/{self.bot_id}/status/custom", {"emoji": "x", "text": "by hand"})
        activity.status_sync([{"name": self.role, "status": "waiting", "waitingFor": "permission"}])
        cs = json.loads(self.owner("GET", f"/users/{self.bot_id}")["props"]["customStatus"])
        self.assertEqual(cs["text"], "by hand")
        self.assertEqual(self.reactions(p["id"]).count("raised_hand"), 1)
        activity.status_sync([{"name": self.role, "status": "idle"}])
        cs = self.owner("GET", f"/users/{self.bot_id}")["props"].get("customStatus")
        self.assertFalse(cs and json.loads(cs).get("text"))

    def test_waiting_notify_once(self):
        agents = [{"sessionId": "s-" + self.role, "name": self.role, "status": "waiting", "waitingFor": "permission"}]
        t0 = time.time()
        activity.waiting_notify(agents, now=t0)
        activity.waiting_notify(agents, now=t0 + 60)
        self.assertEqual(self.dm_posts(), [])
        activity.waiting_notify(agents, now=t0 + 130)
        activity.waiting_notify(agents, now=t0 + 400)
        posts = self.dm_posts()
        self.assertEqual(len(posts), 1)
        self.assertIn(f"**{self.role}** is waiting for permission 2 min 10 s", posts[0])
        activity.waiting_notify([], now=t0 + 500)
        self.assertEqual(json.loads((activity.activity_dir() / "waiting.json").read_text()), {})

    def dm_posts(self):
        r = self.owner("GET", f"/channels/{self.dm}/posts")
        return [r["posts"][i]["message"] for i in r["order"] if r["posts"][i]["user_id"] == self.bot_id
                and "waiting" in r["posts"][i]["message"]]  # not the server's "add me to teams" greeting

    def test_reaction_in_background(self):
        """Without LMOST_ACTIVITY_SYNC the reaction goes from a detached `lmost activity react` process."""
        os.environ.pop("LMOST_ACTIVITY_SYNC")
        p = self.owner("POST", "/posts", {"channel_id": self.dm, "message": "bg"})
        activity.react("it", self.role, p["id"], "eyes")
        for _ in range(50):
            if self.reactions(p["id"]):
                break
            time.sleep(0.1)
        self.assertEqual(self.reactions(p["id"]), ["eyes"])

    def test_same_thread_continues_the_turn(self):
        root = self.owner("POST", "/posts", {"channel_id": self.dm, "message": "first"})
        activity.begin(self.role, {"via": "it", "post": root["id"], "text": "first", "channel": "dm", "role": "owner"})
        since = activity.turn(self.role)["since"]
        more = self.owner("POST", "/posts", {"channel_id": self.dm, "message": "and also", "root_id": root["id"]})
        activity.begin(self.role, {"via": "it", "post": more["id"], "thread": root["id"], "text": "and also",
                                   "channel": "dm", "role": "owner"})
        t = activity.turn(self.role)
        self.assertEqual((t["since"], t["text"], t["root"]), (since, "and also", root["id"]))
        activity.end(self.role)
        self.assertIn("white_check_mark", self.reactions(root["id"]))
        self.assertIn("white_check_mark", self.reactions(more["id"]))


class Wired(IsolatedTest):
    def test_inbox_ack_starts_a_turn(self):
        from lmost import inbox
        self.assertIn(activity.on_ack, inbox.ACK_LISTENERS)
