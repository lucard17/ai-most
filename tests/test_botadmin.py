"""`aimost bot add` (status spec: one bot per role; bots for other people) — names offline, the rest on the real server."""

from __future__ import annotations

import io
import os
import stat
import uuid

from helpers import IsolatedTest
from itenv import IT, requires_mattermost

from aimost import botadmin
from aimost import mattermost as mm


class Names(IsolatedTest):
    def test_prefix_rules(self):
        c = {"NAME": "x", "OWNER": "ann"}
        self.assertEqual(botadmin.bot_name(c, "deploy"), "ann-deploy")
        self.assertEqual(botadmin.bot_name(c, "ann-deploy"), "ann-deploy")
        self.assertEqual(botadmin.bot_name(c, "deploy", prefix="ax"), "ax-deploy")
        self.assertEqual(botadmin.bot_name(c, "deploy", owner="bob"), "bob-deploy")
        self.assertEqual(botadmin.bot_name(c, "deploy", shared=True), "deploy")

    def test_refusals(self):
        for kw, role, conn in (({}, "Bad Name", {"NAME": "x", "OWNER": "a"}), ({}, "deploy", {"NAME": "x"}),
                               ({"shared": True, "owner": "bob"}, "deploy", {"NAME": "x"}),
                               ({"owner": "Bob!"}, "deploy", {"NAME": "x"})):
            with self.assertRaises(botadmin.Refused) as e:
                botadmin.bot_name(conn, role, **kw)
            self.assertEqual(e.exception.code, 2)

    def test_description_required(self):
        with self.assertRaises(botadmin.Refused) as e:
            botadmin.add({"NAME": "x", "OWNER": "a"}, lambda *a: None, "deploy", "  ")
        self.assertEqual(e.exception.code, 2)

    def test_admit_session(self):
        p = self.tmp / "c.env"
        p.write_text("URL=u\nSESSIONS=a, b\n")
        self.assertTrue(botadmin.admit_session(p, "c"))
        self.assertFalse(botadmin.admit_session(p, "a"))
        self.assertIn("SESSIONS=a,b,c\n", p.read_text())
        q = self.tmp / "d.env"
        q.write_text("URL=u\n")
        botadmin.admit_session(q, "z")
        self.assertEqual(q.read_text(), "URL=u\nSESSIONS=z\n")

    def test_team_refusal_is_code_5(self):
        def api(method, path, body=None):
            if path.startswith("/users/username/"):
                raise mm.HTTPError(path, 404, "nf", {}, io.BytesIO(b"{}"))
            if path == "/bots":
                return {"user_id": "b1"}
            if path.startswith("/teams/name/"):
                return {"id": "t1"}
            if path == "/teams/t1/members":
                raise mm.HTTPError(path, 400, "full", {}, io.BytesIO(b'{"message": "team is full"}'))
            raise AssertionError(path)
        with self.assertRaises(botadmin.Refused) as e:
            botadmin.add({"NAME": "x", "OWNER": "a", "TEAM": "t"}, api, "deploy", "does")
        self.assertEqual(e.exception.code, 5)
        self.assertIn("team is full", str(e.exception))
        self.assertFalse(botadmin.token_path({"NAME": "x"}, "deploy").exists())


@requires_mattermost
class Real(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.conn = {"NAME": "it", "URL": IT["AIMOST_IT_URL"]}
        self.pre = "b" + uuid.uuid4().hex[:6]
        cfg = self.tmp / "config"
        (cfg / "connections").mkdir(parents=True)
        self.env_file = cfg / "connections" / "it.env"
        self.env_file.write_text(f"URL={IT['AIMOST_IT_URL']}\nTEAM={IT['AIMOST_IT_TEAM']}\nOWNER={self.pre}\nSESSIONS=x\n")
        self.made = []

    def tearDown(self):
        for n in self.made:
            u = self.owner("GET", f"/users/username/{n}")
            if u.get("is_bot"):
                self.owner("POST", f"/bots/{u['id']}/disable")
        super().tearDown()

    def owner(self, method, path, body=None):
        return mm.api(self.conn, method, path, body, token=IT["AIMOST_IT_TOKEN_OWNER"])

    def add(self, *args):
        return self.aimost("bot", "add", *args, "--via", "it",
                          env={"AIMOST_ADMIN_TOKEN": IT["AIMOST_IT_TOKEN_OWNER"], "AIMOST_NO_RESTART": "1"})

    def test_own_bot_then_retry_then_bring_back(self):
        name = f"{self.pre}-scout"
        self.made.append(name)
        r = self.add("scout", "--desc", "finds things", "--channel", "agents")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"checked: @{name}", r.stdout)
        tp = self.tmp / "config" / "bots" / "it" / "scout"
        tok = tp.read_text()
        self.assertNotIn(tok, r.stdout + r.stderr)
        self.assertEqual(stat.S_IMODE(tp.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(tp.parent.stat().st_mode), 0o700)
        self.assertIn("SESSIONS=x,scout", self.env_file.read_text())
        u = self.owner("GET", f"/users/username/{name}")
        self.assertEqual(self.owner("GET", f"/bots/{u['id']}")["description"], "finds things")
        self.owner("GET", f"/channels/{IT['AIMOST_IT_CHANNEL_AGENTS']}/members/{u['id']}")

        r = self.add("scout", "--desc", "finds things")
        self.assertEqual(r.returncode, 1)

        self.owner("POST", f"/bots/{u['id']}/disable")
        tp.unlink()
        r = self.add("scout", "--desc", "finds more")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(tp.read_text(), tok)
        b = self.owner("GET", f"/bots/{u['id']}")
        self.assertEqual((b["delete_at"], b["description"]), (0, "finds more"))
        self.assertEqual(self.env_file.read_text().count("scout"), 1)

    def test_person_login_refused(self):
        r = self.add("colleague", "--shared", "--desc", "x")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertFalse(self.owner("GET", "/users/username/colleague").get("is_bot"))

    def test_bot_for_a_colleague(self):
        before = self.env_file.read_text()
        name = f"{self.pre}-helper"
        self.made.append(name)
        r = self.add("helper", "--owner", "colleague", "--prefix", self.pre, "--desc", "helps")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"/bot-token {name}", r.stdout)
        u = self.owner("GET", f"/users/username/{name}")
        self.assertEqual(self.owner("GET", f"/bots/{u['id']}")["owner_id"], IT["AIMOST_IT_USER_ID_COLLEAGUE"])
        self.assertFalse((self.tmp / "config" / "bots").exists(), "no token is issued for someone else's bot")
        self.assertEqual(self.env_file.read_text(), before)

        r = self.add("helper", "--owner", "outsider", "--prefix", self.pre, "--desc", "helps")
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertEqual(self.owner("GET", f"/bots/{u['id']}")["owner_id"], IT["AIMOST_IT_USER_ID_COLLEAGUE"])

    def test_owner_must_be_a_person(self):
        self.assertEqual(self.add("hh", "--owner", "deployer", "--desc", "x").returncode, 6)
        self.assertEqual(self.add("h2", "--owner", "nobody-" + self.pre, "--desc", "x").returncode, 6)

    def test_no_admin_token(self):
        r = self.aimost("bot", "add", "scout", "--desc", "x", "--via", "it")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no bot of the admin role", r.stderr, "no admin token — the bot is asked for through the edge")
