"""``aimost post`` against the disposable real Mattermost (specs: text, questions, delivery → relay).

Each test class makes its own channel (uuid name) in the shared team; nothing of other suites is touched.
"""

from __future__ import annotations

import json
import os
import unittest
import uuid

from helpers import IsolatedTest
from itenv import IT, requires_mattermost

from aimost import askstore, cli_post, mattermost as mm, paths, post, questions

def owner_call(method, path, body=None, token=None):
    return mm.api({"URL": IT["AIMOST_IT_URL"]}, method, path, body, token=token or IT["AIMOST_IT_TOKEN_OWNER"])


def bot_call(role, method, path, body=None):
    return mm.api({"URL": IT["AIMOST_IT_URL"]}, method, path, body, token=IT[f"AIMOST_IT_BOT_{role.upper()}"])


def new_channel(members=("deployer",), people=("owner",)) -> dict:
    ch = owner_call("POST", "/channels", {"team_id": IT["AIMOST_IT_TEAM_ID"], "name": "t" + uuid.uuid4().hex[:20],
                                          "display_name": "test", "type": "O"})
    for b in members:
        owner_call("POST", f"/channels/{ch['id']}/members", {"user_id": IT[f"AIMOST_IT_BOT_ID_{b.upper()}"]})
    for u in people:
        if u != "owner":
            owner_call("POST", f"/channels/{ch['id']}/members", {"user_id": IT[f"AIMOST_IT_USER_ID_{u.upper()}"]})
    return ch


class World(IsolatedTest):
    """A connection ``it`` to the disposable server: deployer and devops have bots, relay is the relay bot."""
    role = "deployer"
    conn_extra = ""

    def setUp(self):
        super().setUp()
        c = paths.config_dir()
        (c / "connections").mkdir(parents=True)
        (c / "connections" / "it.env").write_text(
            f"URL={IT['AIMOST_IT_URL']}\nOWNER=owner\nTEAM=it\nSESSIONS=deployer,devops,probe,other\nRELAY=relay\n"
            f"EDGE_PUBLIC=edge.test:8767\nEDGE_SECRET=sec\nEDGE_KEY_ID=k1\n{self.conn_extra}")
        (c / "bots" / "it").mkdir(parents=True)
        for b in ("deployer", "devops", "relay"):
            (c / "bots" / "it" / b).write_text(IT[f"AIMOST_IT_BOT_{b.upper()}"])
        os.environ["AIMOST_ROLE"] = self.role

    def run_post(self, *args, role=None):
        r = self.aimost("post", *args, env={"AIMOST_ROLE": role or self.role})
        return r.returncode, r.stdout, r.stderr

    def ok(self, *args, role=None) -> str:
        rc, out, err = self.run_post(*args, role=role)
        self.assertEqual(rc, 0, err)
        return out

    def refused(self, code, *args, role=None) -> str:
        rc, out, err = self.run_post(*args, role=role)
        self.assertEqual(rc, code, out + err)
        self.assertNotIn("posted", out)
        return err

    @staticmethod
    def post_id(out: str) -> str:
        return out.split("post ")[1].split(",")[0].split(")")[0]

    def get(self, pid):
        return owner_call("GET", f"/posts/{pid}")

    def dm_id(self, bot="deployer", user="OWNER"):
        return owner_call("POST", "/channels/direct",
                          [IT[f"AIMOST_IT_BOT_ID_{bot.upper()}"], IT[f"AIMOST_IT_USER_ID_{user}"]])["id"]


class Args(unittest.TestCase):
    def test_flags_before_and_after_ask(self):
        o, f, fl, a = cli_post.parse(["--via", "it", "--ask", "q.json", "--to", "ann,bob", "--blocking"])
        self.assertEqual((o, fl, a), ({"via": "it", "to": "ann,bob"}, {"blocking"}, ["--ask", "q.json"]))
        o, f, fl, a = cli_post.parse(["--ask", "Q?", "yes", "no", "--rec", "1", "--to", "ann", "--file", "x"])
        self.assertEqual((o, f, a), ({"to": "ann"}, ["x"], ["--ask", "Q?", "yes", "no", "--rec", "1"]))
        o, f, fl, a = cli_post.parse(["--plain", "--file", "a.md", "--file", "b.png", "text --to x"])
        self.assertEqual((f, fl, a), (["a.md", "b.png"], {"plain"}, ["text --to x"]))


class Offline(IsolatedTest):
    def test_usage_errors_before_network(self):
        for args, code in ((["--blocking", "x"], 2), (["--ttl", "0", "--ask", "Q?", "a", "b"], 2),
                           (["--ask", "Q?", "a"], 2), (["--ask", "x" * 301, "a", "b"], 4), (["x"], 2)):
            r = self.aimost("post", *args, env={"AIMOST_ROLE": "nobody" if args != ["x"] else ""})
            self.assertEqual(r.returncode, code, (args, r.stderr))


@requires_mattermost
class Basics(World):
    def test_dm_by_default(self):
        pid = self.post_id(self.ok("hello owner"))
        p = self.get(pid)
        self.assertEqual((p["message"], p["channel_id"], p["props"]["agent"]), ("hello owner", self.dm_id(), "deployer"))

    def test_dm_with_person_and_thread_channel(self):
        pid = self.post_id(self.ok("--channel", "dm:colleague", "hi"))
        self.assertEqual(self.get(pid)["channel_id"], self.dm_id(user="COLLEAGUE"))
        rid = self.post_id(self.ok("--thread", pid, "reply"))
        r = self.get(rid)
        self.assertEqual((r["root_id"], r["channel_id"]), (pid, self.dm_id(user="COLLEAGUE")))

    def test_not_admitted_and_no_bot(self):
        self.refused(3, "x", role="stranger")
        rc, out, err = self.run_post("--channel", "general", "x", role="other")    # admitted, no bot → relay → refuse
        self.assertEqual(rc, 3, err)

    def test_two_questions_as_text(self):
        self.refused(4, "Deploy? Or wait?")
        self.ok("--plain", "log: Deploy? Or wait?")

    def test_wall_of_text(self):
        wall = "We looked at the failing upload and " + "found that the retry loop gives up too early, " * 12
        err = self.refused(11, wall)
        self.assertIn("the first line", err)
        self.ok("--plain", wall)
        doc = self.tmp / "notes.md"
        doc.write_text(wall + "\n")
        self.ok("--file", str(doc))                       # a document goes as written
        pid = self.post_id(self.ok("A short point."))
        self.assertEqual(self.run_post("--edit", pid, wall)[0], 11, "an edit too")

    def test_unknown_login(self):
        err = self.refused(6, "ping @nosuchperson")
        self.assertIn("@nosuchperson", err)
        self.ok("code `@nosuchperson` is fine")

    def test_private_guard(self):
        with (paths.config_dir() / "connections" / "it.env").open("a") as f:
            f.write("PRIVATE_GUARD=on\n")
        self.refused(5, "x")
        (paths.config_dir() / "private-patterns").write_text("my-hobby\n")
        self.refused(5, "news about my-hobby")
        pid = self.post_id(self.ok("work news"))
        self.refused(5, "--edit", pid, "now about my-hobby")
        self.refused(5, "--ask", "Join my-hobby?", "yes", "no")
        self.assertEqual(self.get(pid)["message"], "work news")


@requires_mattermost
class Channels(World):
    def setUp(self):
        super().setUp()
        self.ch = new_channel()

    def test_members_invite_and_bot_not_in_channel(self):
        err = self.refused(7, "--channel", self.ch["name"], "@colleague look")
        self.assertIn("@colleague", err)
        pid = self.post_id(self.ok("--channel", self.ch["name"], "--invite", "@colleague look"))
        self.assertEqual(self.get(pid)["channel_id"], self.ch["id"])
        err = self.refused(8, "--channel", self.ch["name"], "x", role="devops")
        self.assertIn("@devops", err)

    def test_no_such_channel(self):
        self.refused(2, "--channel", "nope-" + uuid.uuid4().hex[:8], "x")

    def test_pin_edited_in_place(self):
        out = self.ok("--channel", self.ch["name"], "--pin", "rules", "rule one")
        self.assertIn("created", out)
        pid = self.post_id(out)
        self.assertIn("unchanged", self.ok("--channel", self.ch["name"], "--pin", "rules", "rule one"))
        self.assertIn("updated", self.ok("--channel", self.ch["name"], "--pin", "rules", "rule two"))
        pinned = owner_call("GET", f"/channels/{self.ch['id']}/pinned")["posts"]
        self.assertEqual(list(pinned), [pid])
        self.assertEqual((pinned[pid]["message"], pinned[pid]["props"]["aimost_pin"]), ("rule two", "rules"))

    def test_edit_own_only(self):
        pid = self.post_id(self.ok("--channel", self.ch["name"], "first"))
        self.ok("--edit", pid, "second")
        self.assertEqual(self.get(pid)["message"], "second")
        self.refused(4, "--edit", pid, "A? B?")
        mine = owner_call("POST", "/posts", {"channel_id": self.ch["id"], "message": "owner's"})
        self.refused(3, "--edit", mine["id"], "hijack")
        self.assertEqual(self.get(mine["id"])["message"], "owner's")


@requires_mattermost
class Files(World):
    def test_md_as_text_without_attachment(self):
        f = self.tmp / "report.md"
        f.write_text("# Result\n\nall good\n")
        p = self.get(self.post_id(self.ok("--file", str(f))))
        self.assertEqual(p["message"], "# Result\n\nall good\n")
        self.assertFalse(p.get("file_ids"))
        p = self.get(self.post_id(self.ok("--file", str(f), "--attach")))
        self.assertEqual(len(p["file_ids"]), 1)

    def test_long_md_in_parts(self):
        f = self.tmp / "long.md"
        f.write_text("\n".join(f"## section {i}\n" + "word " * 1500 for i in range(4)))
        out = self.ok("--file", str(f))
        self.assertIn("parts)", out)
        pid = self.post_id(out)
        th = owner_call("GET", f"/posts/{pid}/thread")
        self.assertGreaterEqual(len(th["order"]), 2)
        self.assertIn("part 1 of", self.get(pid)["message"])

    def test_attachment_with_text(self):
        f = self.tmp / "data.txt"
        f.write_text("1,2,3\n")
        p = self.get(self.post_id(self.ok("--file", str(f), "numbers")))
        self.assertEqual((p["message"], len(p["file_ids"])), ("numbers", 1))

    def test_mermaid_image_into_thread(self):
        fake = self.tmp / "mmdc"
        fake.write_text("#!/bin/sh\nprintf 'PNG' > \"$2\"\n")
        fake.chmod(0o755)
        r = self.aimost("post", "graph:\n```mermaid\ngraph TD; A-->B\n```",
                       env={"AIMOST_ROLE": "deployer", "AIMOST_MERMAID_BIN": str(fake),
                            "AIMOST_MERMAID_DIR": str(self.tmp / "mm")})
        self.assertEqual(r.returncode, 0, r.stderr)
        th = owner_call("GET", f"/posts/{self.post_id(r.stdout)}/thread")
        imgs = [p for p in th["posts"].values() if p.get("file_ids")]
        self.assertEqual(len(imgs), 1)
        self.assertIn("for phones", imgs[0]["message"])


@requires_mattermost
class TurnThread(World):
    def turn(self, **kw):
        d = self.tmp / "state" / "activity"
        d.mkdir(parents=True, exist_ok=True)
        (d / "deployer.json").write_text(json.dumps({"via": "it", **kw}))

    def test_post_goes_into_the_turn_thread(self):
        root = self.post_id(self.ok("--new-thread", "turn root"))
        self.turn(root=root, ended=None)
        rc, out, err = self.run_post("reply")
        self.assertEqual(self.get(self.post_id(out))["root_id"], root)
        self.assertIn(f"into the turn's thread {root}", err)
        rc, out, err = self.run_post("--new-thread", "new topic")
        self.assertEqual(self.get(self.post_id(out))["root_id"], "")
        self.turn(root=root, ended=1)
        self.assertEqual(self.get(self.post_id(self.ok("after")))["root_id"], "")

    def test_turn_author_mentioned_in_channel(self):
        ch = new_channel(people=("owner", "colleague"))
        root = owner_call("POST", "/posts", {"channel_id": ch["id"], "message": "@deployer status?"},
                          token=IT["AIMOST_IT_TOKEN_COLLEAGUE"])
        self.turn(root=root["id"], ended=None, **{"from": "colleague", "role": "person"})
        pid = self.post_id(self.ok("--thread", root["id"], "--channel", "dm", "all green"))
        self.assertEqual(self.get(pid)["message"], "@colleague all green")
        self.turn(root=root["id"], ended=None, **{"from": "devops", "role": "peer"})
        pid = self.post_id(self.ok("--thread", root["id"], "all green"))
        self.assertEqual(self.get(pid)["message"], "all green")


@requires_mattermost
class Asks(World):
    def test_dm_ask_to_owner(self):
        out = self.ok("--ask", "Deploy now?", "yes", "no", "--rec", "1")
        pid = self.post_id(out)
        qid = out.split("question ")[1].split()[0]
        p = self.get(pid)
        props = p["props"]
        self.assertEqual((props["aimost_question"], props["aimost_ask"]["to"], props["aimost_ask"]["discuss"]),
                         (qid, ["owner"], False))
        acts = props["attachments"][0]["actions"]
        # the server hides button integrations (url, signed context) from readers; their shape — test_questions
        self.assertEqual([a["name"] for a in acts], ["⭐ yes", "no", questions.CUSTOM])
        self.assertNotIn("integration", acts[0])
        rec = askstore.asks("deployer")[qid]
        self.assertEqual((rec["post"], rec["via"], rec["state"], rec["to"]), (pid, "it", "open", ["owner"]))

    def test_channel_ask_needs_to_and_flags_after_ask(self):
        ch = new_channel(people=("owner", "colleague"))
        self.refused(2, "--channel", ch["name"], "--ask", "Merge?", "yes", "no")
        spec = self.tmp / "q.json"
        spec.write_text(json.dumps({"context": "", "parts": [{"title": "Merge?", "options": ["yes", "no"]},
                                                             {"title": "Notify?", "options": ["a", "b"]}]}))
        out = self.ok("--channel", ch["name"], "--ask", str(spec), "--to", "owner,colleague")
        p = self.get(self.post_id(out))
        self.assertEqual(p["props"]["aimost_ask"]["to"], ["owner", "colleague"])
        self.assertTrue(p["props"]["aimost_ask"]["discuss"])
        self.assertEqual(p["message"].split("\n")[:3], ["@owner @colleague", "❓ 1. Merge?", "2. Notify?"])

    def test_bot_addressee_and_non_member(self):
        ch = new_channel()
        self.assertIn("bot does not press", self.refused(6, "--channel", ch["name"], "--ask", "Q?", "a", "b",
                                                          "--to", "devops"))
        self.refused(7, "--channel", ch["name"], "--ask", "Q?", "a", "b", "--to", "colleague")
        self.ok("--channel", ch["name"], "--ask", "Q?", "a", "b", "--to", "colleague", "--invite")

    def test_long_question_refused(self):
        self.refused(4, "--ask", "x" * 301, "a", "b")

    def test_cancel_open_asks(self):
        out1 = self.ok("--ask", "Drop the flag?", "yes", "no", "--ttl", "2")
        out2 = self.ok("--ask", "Keep?", "yes", "no")
        q1, p1 = out1.split("question ")[1].split()[0], self.post_id(out1)
        q2 = out2.split("question ")[1].split()[0]
        listed = self.ok("--open-asks")
        self.assertIn(q1, listed)
        self.assertIn("deadline 2 h", listed)
        self.assertIn("cancelled", self.ok("--cancel", q1, "moot now"))
        p = self.get(p1)
        self.assertTrue(p["message"].startswith("⏹ Question cancelled: moot now\n"))
        self.assertNotIn("actions", p["props"]["attachments"][0])
        self.assertEqual(p["props"]["aimost_cancelled"]["why"], "moot now")
        self.assertEqual((p["props"]["agent"], p["props"]["aimost_ask"]["to"]), ("deployer", ["owner"]))
        self.assertIn("already cancelled", self.ok("--cancel", p1, "again"))
        listed = self.ok("--open-asks")
        self.assertNotIn(q1, listed)
        self.assertIn(q2, listed)
        self.refused(2, "--cancel", "ask-00000000", "x")
        ch = new_channel(members=("deployer", "devops"), people=("owner", "colleague"))
        theirs = self.post_id(self.ok("--channel", ch["name"], "--ask", "Q?", "a", "b", "--to", "colleague"))
        self.refused(3, "--cancel", theirs, "not mine", role="devops")
        self.refused(2, "--cancel", self.post_id(out2), "closed to devops", role="devops")

    def test_answered_or_deleted_leaves_the_list(self):
        out = self.ok("--ask", "Done?", "yes", "no")
        qid, pid = out.split("question ")[1].split()[0], self.post_id(out)
        p = self.get(pid)
        props = dict(p["props"], aimost_answers={"1": {"option": 1, "by": "owner"}})
        bot_call("deployer", "PUT", f"/posts/{pid}/patch", {"props": props})
        self.assertNotIn(qid, self.ok("--open-asks"))
        self.assertEqual(askstore.asks("deployer")[qid]["state"], "answered")

    def test_sink_question_through_the_api(self):
        lines = post.Poster("deployer").ask({"parts": [{"title": "Approve?", "options": ["yes", "no"]}]},
                                             sink="tracker")
        qid = lines[1].split("question ")[1].split()[0]
        self.assertEqual(askstore.asks("deployer")[qid]["sink"], "tracker")
        self.assertEqual(self.get(self.post_id(lines[0]))["props"]["aimost_ask"]["sink"], "tracker")


@requires_mattermost
class Relay(World):
    role = "probe"

    def test_relay_posts_signed_into_owner_dm_only(self):
        pid = self.post_id(self.ok("hello"))
        p = self.get(pid)
        self.assertEqual(p["message"], "from session probe · via relay relay\nhello")
        self.assertEqual((p["channel_id"], p["props"]["relay"], p["props"]["agent"]),
                         (self.dm_id("relay"), "relay", "probe"))
        self.refused(3, "--channel", "agents", "x")
        self.refused(3, "--channel", "dm:colleague", "x")
        # editing keeps the signature; another session's relay post is not ours
        self.ok("--edit", pid, "hello again")
        self.assertEqual(self.get(pid)["message"], "from session probe · via relay relay\nhello again")
        self.refused(3, "--edit", pid, "hijack", role="other")
        # own thread — yes; another session's thread — no
        self.ok("--thread", pid, "more")
        self.refused(3, "--thread", pid, "intrude", role="other")

    def test_relay_ask_signed(self):
        out = self.ok("--ask", "Go?", "yes", "no")
        p = self.get(self.post_id(out))
        self.assertTrue(p["message"].startswith("❓ from session probe · via relay relay"))
        self.assertTrue(p["props"]["attachments"][0]["actions"])

    def test_own_bot_wins(self):
        p = self.get(self.post_id(self.ok("hi", role="deployer")))
        self.assertNotIn("relay", p["props"])


@requires_mattermost
class Conclude(World):
    def test_conclude_own_discussion(self):
        ch = new_channel(people=("owner",))
        q = owner_call("POST", "/posts", {"channel_id": ch["id"], "message": "question"})
        dm = self.dm_id()
        droot = bot_call("deployer", "POST", "/posts", {"channel_id": dm, "message": "Discussion: question"})
        d = paths.link_state_dir()
        d.mkdir(parents=True)
        askstore.discussions_path("it").write_text(json.dumps({droot["id"]: {
            "asker": "deployer", "channel": ch["id"], "thread": q["id"], "user": "owner", "dm_channel": dm}}))
        self.refused(3, "--conclude", droot["id"], "done", role="devops")
        self.ok("--conclude", droot["id"], "we agreed  to ship")
        th = owner_call("GET", f"/posts/{q['id']}/thread")["posts"]
        self.assertIn("💬 Outcome of the discussion with @owner: we agreed to ship",
                      [p["message"] for p in th.values()])
        dth = owner_call("GET", f"/posts/{droot['id']}/thread")["posts"]
        self.assertTrue(any(p["message"].startswith("✅ outcome sent") for p in dth.values()))


if __name__ == "__main__":
    unittest.main()
