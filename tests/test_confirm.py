"""Confirmation by a button (authority: "Dangerous actions in a chat-started turn need a button", "Who confirms",
"A confirmation mentions who can press it"). Oracle: upstream chat-confirm.test.sh, chat-confirm-confirmers.test.sh.
The post-and-wait part runs against the disposable real Mattermost; the edge's press is played by the owner patching
the post's props the way the edge does."""

from __future__ import annotations

import os
import threading
import time
import unittest
import uuid

from helpers import IsolatedTest
from itenv import IT, requires_mattermost

from aimost import confirm, mattermost as mm, paths


class Patterns(IsolatedTest):
    def test_defaults_and_user_file_add_up(self):
        self.assertIsNotNone(confirm.match("terraform apply -auto-approve", confirm.patterns("patterns")))
        self.assertIsNone(confirm.match("git log", confirm.patterns("patterns")))
        cfg = paths.config_dir()
        cfg.mkdir(parents=True)
        (cfg / "confirm-patterns").write_text("# mine\n\\bmy-deploy\\b\n(broken\n")
        pats = confirm.patterns("patterns")
        self.assertEqual(confirm.match("bin/my-deploy prod", pats), r"\bmy-deploy\b")
        self.assertIsNotNone(confirm.match("glab mr merge 5", pats), "a user file cannot drop a default")
        self.assertIsNone(confirm.match("ls (", pats), "a broken pattern is skipped")

    def test_deny_default(self):
        deny = confirm.patterns("deny")
        self.assertIsNotNone(confirm.match("psql -h db -c 'DELETE FROM t'", deny))
        self.assertIsNone(confirm.match("psql -h db -c 'SELECT 1'", deny))


class Records(IsolatedTest):
    def rec(self, cid, at_offset=0, used=False):
        confirm.save("deployer", {"id": cid, "action": "deploy", "at": time.time() + at_offset, "used": used})

    def test_fresh_spent_once(self):
        self.rec("a1b2c3d4")
        self.assertIsNone(confirm.spend("deployer", "a1b2c3d4"))
        self.assertEqual(confirm.spend("deployer", "a1b2c3d4"), "used or older than 15 minutes")

    def test_old_missing_foreign(self):
        self.rec("0ld0ld00".replace("l", "1"), at_offset=-1000)
        self.assertEqual(confirm.spend("deployer", "01d01d00"), "used or older than 15 minutes")
        self.assertEqual(confirm.spend("deployer", "ffffffff"), "missing")
        self.rec("abcdabcd")
        self.assertEqual(confirm.spend("other", "abcdabcd"), "missing", "another role's confirmation does not count")
        self.assertEqual(confirm.spend("deployer", "../x"), "missing")

    def test_record_private(self):
        self.rec("a1b2c3d4")
        self.assertEqual(os.stat(confirm.record_path("deployer", "a1b2c3d4")).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(paths.marks_dir() / "deployer").st_mode & 0o777, 0o700)


class Render(unittest.TestCase):
    def test_buttons_and_outcome(self):
        msg, atts = confirm.render_confirm("c1", "deploy api", "v1.2, host-1", "deployer", "http://edge:8767/action",
                                           {"secret": "s", "kid": "k1"}, to=["owner", "bob"])
        self.assertTrue(msg.startswith("🛡 **Confirm?** deploy api\nv1.2, host-1"))
        acts = atts[0]["actions"]
        self.assertEqual([a["id"] for a in acts], ["yes", "no"])
        self.assertEqual(acts[0]["integration"]["url"], "http://edge:8767/action")
        ctx = acts[0]["integration"]["context"]
        self.assertEqual((ctx["kind"], ctx["id"], ctx["answer"], ctx["secret"], ctx["kid"], ctx["to"]),
                         ("confirm", "c1", "yes", "s", "k1", ["owner", "bob"]))
        msg, atts = confirm.render_confirm("c1", "deploy api", "", "deployer", "https://edge.example/action", {},
                                           done={"answer": "no", "by": "owner"})
        self.assertEqual((msg, atts), ("🛡 **Confirm?** deploy api\n\n❌ declined — @owner", []))


@requires_mattermost
class RealServer(IsolatedTest):
    """chat-confirm against a real server: a channel thread where the owner, a confirmer and an outsider are present."""

    @classmethod
    def setUpClass(cls):
        cls.conn = {"NAME": "it", "URL": IT["AIMOST_IT_URL"]}
        cls.owner_tok = IT["AIMOST_IT_TOKEN_OWNER"]
        cls.bot = IT["AIMOST_IT_BOT_DEPLOYER"]
        name = "c-" + uuid.uuid4().hex[:12]
        cls.channel = mm.api(cls.conn, "POST", "/channels", {"team_id": IT["AIMOST_IT_TEAM_ID"], "name": name,
                                                             "display_name": name, "type": "O"}, token=cls.owner_tok)
        for uid in (IT["AIMOST_IT_BOT_ID_DEPLOYER"], IT["AIMOST_IT_BOT_ID_DEVOPS"], IT["AIMOST_IT_USER_ID_COLLEAGUE"], IT["AIMOST_IT_USER_ID_OUTSIDER"]):
            mm.api(cls.conn, "POST", f"/channels/{cls.channel['id']}/members", {"user_id": uid}, token=cls.owner_tok)

    def setUp(self):
        super().setUp()
        # a thread of its own per test: the bot's root, then the confirmer and an outsider reply
        self.root = mm.api(self.conn, "POST", "/posts", {"channel_id": self.channel["id"], "message": "task"},
                           token=self.bot)
        for tok in (IT["AIMOST_IT_TOKEN_COLLEAGUE"], IT["AIMOST_IT_TOKEN_OUTSIDER"]):
            mm.api(self.conn, "POST", "/posts", {"channel_id": self.channel["id"], "message": "hi",
                                                 "root_id": self.root["id"]}, token=tok)
        os.environ["AIMOST_CONFIRM_EVERY_S"] = "0.2"
        self.addCleanup(os.environ.pop, "AIMOST_CONFIRM_EVERY_S", None)
        cfg = paths.config_dir()
        (cfg / "connections").mkdir(parents=True)
        (cfg / "bots" / "it").mkdir(parents=True)
        (cfg / "connections" / "it.env").write_text(
            f"URL={IT['AIMOST_IT_URL']}\nOWNER=owner\nTEAM={IT['AIMOST_IT_TEAM']}\nSESSIONS=deployer,devops\n"
            "EDGE_SECRET=s3\nEDGE_PUBLIC=edge:8767\nCONFIRMERS=deployer:colleague\n")
        (cfg / "bots" / "it" / "deployer").write_text(self.bot)
        (cfg / "bots" / "it" / "devops").write_text(IT["AIMOST_IT_BOT_DEVOPS"])

    def run_request(self, role="deployer", **kw):
        out = {}

        def go():
            try:
                out["id"] = confirm.request(role, "deploy api", "v1.2", thread=self.root["id"], **kw)
            except confirm.ConfirmError as e:
                out["code"] = e.code
            except Exception as e:  # surfaced by the assertions on out
                out["error"] = repr(e)
        t = threading.Thread(target=go)
        t.start()
        return t, out

    def wait_post(self):
        for _ in range(100):
            posts = mm.api(self.conn, "GET", f"/posts/{self.root['id']}/thread", token=self.owner_tok)["posts"]
            hit = [p for p in posts.values() if (p.get("props") or {}).get("aimost_confirm_request")
]
            if hit:
                return hit[0]
            time.sleep(0.1)
        self.fail("no confirmation post")

    def press(self, post, user_id, login, answer="yes"):
        """What the edge does on a press: patch the post's props with the choice (props are replaced whole)."""
        props = dict(post["props"], aimost_confirm={"id": post["props"]["aimost_confirm_request"], "answer": answer,
                                                   "by": login, "user_id": user_id, "at": int(time.time() * 1000)})
        mm.api(self.conn, "PUT", f"/posts/{post['id']}/patch", {"props": props}, token=self.owner_tok)

    def test_confirmer_press_counts_and_mentions(self):
        t, out = self.run_request(timeout=20)
        post = self.wait_post()
        self.assertEqual(post["message"].split("\n")[0], "@owner @colleague",
                         "owner and the confirmer who wrote in the thread; the outsider is not mentioned")
        # The server hides button integrations (url, context) from GET — the context is checked offline (Render).
        self.assertNotIn("integration", post["props"]["attachments"][0]["actions"][0])
        self.press(post, IT["AIMOST_IT_USER_ID_COLLEAGUE"], "colleague")
        t.join(20)
        self.assertIn("id", out, out)
        self.assertIsNone(confirm.spend("deployer", out["id"]), "the record is fresh and unused")

    def test_outsider_press_does_not_count_and_expires(self):
        t, out = self.run_request(timeout=3)
        post = self.wait_post()
        self.press(post, IT["AIMOST_IT_USER_ID_OUTSIDER"], "outsider")
        t.join(20)
        self.assertEqual(out.get("code"), 2)
        msg = mm.api(self.conn, "GET", f"/posts/{post['id']}", token=self.owner_tok)["message"]
        self.assertTrue(msg.endswith("⌛ expired without an answer"), msg)

    def test_owner_declines(self):
        t, out = self.run_request(timeout=20)
        post = self.wait_post()
        self.press(post, IT["AIMOST_IT_USER_ID_OWNER"], "owner", answer="no")
        t.join(20)
        self.assertEqual(out.get("code"), 1)

    def test_other_roles_confirmer_only_owner_mentioned(self):
        t, out = self.run_request(role="devops", timeout=2)
        post = self.wait_post()
        self.assertEqual(post["message"].split("\n")[0], "@owner")
        self.press(post, IT["AIMOST_IT_USER_ID_COLLEAGUE"], "colleague")
        t.join(20)
        self.assertEqual(out.get("code"), 2, "a confirmer of another role does not count")

    def test_to_outside_allowed_no_post(self):
        with self.assertRaises(confirm.ConfirmError) as e:
            confirm.request("deployer", "x", thread=self.root["id"], to=["outsider"])
        self.assertEqual(e.exception.code, 2)
        self.assertIn("outsider", str(e.exception))

    def test_not_admitted_or_no_bot_exit_3(self):
        r = self.aimost("confirm", "deploy", env={"AIMOST_ROLE": "stranger"})
        self.assertEqual(r.returncode, 3, r.stderr)


if __name__ == "__main__":
    unittest.main()
