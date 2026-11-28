"""The optional ``admin`` plugin (admin spec; Oracle: upstream chat-edge-admin.test.sh): the closed action list, the dry
run and the confirmation post, the press by a confirmer on its own post, the fingerprint, copies before deleting,
rollbacks (24 h, once, never into a private channel), failures midway, the request side and ``aimost bot add`` without an
admin token."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from helpers import IsolatedTest
from fakechat import Chat

from aimost import edge as edgemod
from aimost.names import P
from aimost_admin import executor, request

ADM, SEC, TOK = "a" * 40, "s" * 40, "cmdtok"


class World(IsolatedTest):
    def setUp(self):
        super().setUp()
        c = self.c = Chat()
        self.team = c.team("t")
        self.ann = c.user("ann")              # the edge owner and the confirmer
        self.bob = c.user("bob")
        self.req_bot = c.user("ann-admin", bot=True, owner=self.ann)
        self.log = c.channel(self.team, "admin-log", "P", {self.ann, self.req_bot, c.me})
        self.pub = c.channel(self.team, "town", "O", {self.ann, self.bob})
        self.priv = c.channel(self.team, "secret", "P", {self.ann, self.bob})
        self.dm = c.channel(self.team, "a__b", "D", {self.ann, self.bob})
        self.env = {"EDGE_SECRET": SEC[:-1] + "x", "EDGE_ADMIN_SECRET": ADM, "EDGE_ADMIN_CONFIRMERS": self.ann,
                    "EDGE_ADMIN_CMD_TOKEN": TOK, "EDGE_ADMIN_LOG_CHANNEL": self.log, "EDGE_ADMIN_REQUESTER": self.req_bot,
                    "EDGE_ADMIN_STATE": str(self.tmp / "state"), "EDGE_OWNER": "ann", "EDGE_PUBLIC": "edge:1"}
        self.lines = []
        self.make_edge()

    def make_edge(self, **env):
        self.e = edgemod.Edge(env=dict(self.env, **env), call=self.c, log=self.lines.append)
        self.adm = executor.Admin(self.e)

    # the flow as Mattermost runs it
    def ask(self, action, target, origin="ann", why="tidy up", channel=None, by=None):
        req = {"action": action, "target": target, "why": why, "origin": origin}
        pid = self.c.post(channel or self.log, by or self.req_bot, "request", props={P("admin"): req})
        out = self.adm.command({"user_id": by or self.req_bot, "token": TOK}, [pid])
        return pid, out

    def confirm_post(self):
        return [p for p in self.c.posts.values() if (p["props"] or {}).get(P("admin_confirm"))][-1]

    def press(self, post=None, answer="yes", user=None, ctx=None):
        post = post or self.confirm_post()
        acts = post["props"]["attachments"][0]["actions"]
        c = ctx or next(a["integration"]["context"] for a in acts if a["integration"]["context"]["answer"] == answer)
        r = self.adm.press({"user_id": user or self.ann, "post_id": post["id"], "context": c})
        if "update" in r:                 # Mattermost applies the update to the post
            post = self.c.posts[post["id"]]
            post["message"], post["props"] = r["update"]["message"], r["update"]["props"]
        return r

    def journal(self):
        p = self.tmp / "state" / "journal.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class Flow(World):
    def test_delete_posts_permanently_after_a_copy(self):
        root = self.c.post(self.pub, self.bob, "spam")
        reply = self.c.post(self.pub, self.ann, "re", root=root)
        other = self.c.post(self.pub, self.bob, "more", files=2)
        _, out = self.ask("posts-delete", {"post_ids": [root, other]})
        self.assertIn("dry run ready", out)
        msg = self.confirm_post()["message"]
        self.assertTrue(msg.startswith("🛠 **admin · posts-delete:** Delete permanently 3 post(s)"), msg)
        self.assertIn("**irreversible**", msg)
        self.assertIn("**Attachments: 2", msg)
        self.assertIn("why: `tidy up`", msg)
        self.assertEqual([root, reply, other], [x for x in (root, reply, other) if x in self.c.posts], "nothing yet")
        r = self.press()
        self.assertIn("✅ done 3 of 3", r["update"]["message"])
        self.assertFalse({root, reply, other} & set(self.c.posts))
        res = r["update"]["props"][P("admin_result")]
        bk = json.loads((self.tmp / "state" / "backup" / res["backup"]).read_text())
        self.assertEqual(sorted(p["id"] for p in bk["data"]["posts"]), sorted([root, reply, other]))
        self.assertEqual([j["result"] for j in self.journal()], ["asked", "started", "done"])
        self.assertTrue(any("admin: posts-delete — ✅ done 3 of 3" in p["message"] for p in self.c.posts.values()
                            if p["channel_id"] == self.log), "a line in the log channel")
        self.assertEqual(self.c.posts[self.confirm_post()["id"]]["props"]["attachments"], [], "the buttons are gone")

    def test_no_copy_no_delete(self):
        p = self.c.post(self.pub, self.bob, "x")
        self.ask("posts-delete", {"post_ids": [p]})
        with mock.patch.object(executor.Admin, "backup_write", side_effect=OSError("disk")):
            r = self.press()
        self.assertIn("the copy was not written", r["ephemeral_text"])
        self.assertIn(p, self.c.posts)
        self.assertFalse(any(m == "DELETE" for m, _ in self.c.calls))

    def test_category_and_its_rollback(self):
        c2 = self.c.channel(self.team, "news", "O", {self.ann})
        own = {"id": "x" * 26, "type": "custom", "display_name": "Mine", "channel_ids": [self.pub]}
        self.c.cats[(self.bob, self.team)] = [own]
        self.ask("sidebar-category", {"team": "t", "name": "Rules", "channels": ["town", "news"], "users": ["ann", "bob"]})
        self.assertIn("People: 2 (create 2, extend 0)", self.confirm_post()["message"])
        r = self.press()
        self.assertIn("done 2 of 2", r["update"]["message"])
        names = {u: [c["display_name"] for c in self.c.cats[(u, self.team)]] for u in (self.ann, self.bob)}
        self.assertEqual(names, {self.ann: ["Rules"], self.bob: ["Mine", "Rules"]})
        res_post = self.confirm_post()["id"]
        self.c.cats[(self.ann, self.team)][0]["channel_ids"].append(self.priv)   # ann put her own channel there
        out = self.adm.command({"user_id": self.req_bot, "token": TOK}, ["rollback", res_post])
        self.assertIn("dry run ready", out)
        self.assertIn("this is a rollback", self.confirm_post()["message"])
        self.press()
        self.assertEqual([c["display_name"] for c in self.c.cats[(self.bob, self.team)]], ["Mine"], "own category stays")
        self.assertEqual([(c["display_name"], c["channel_ids"]) for c in self.c.cats[(self.ann, self.team)]],
                         [("Rules", [self.priv])], "created by the edge, but holds her channel now — kept")
        self.assertIn("already rolled back", self.adm.command({"user_id": self.req_bot, "token": TOK},
                                                              ["rollback", res_post]))
        del c2

    def test_member_add_and_remove(self):
        carol = self.c.user("carol")
        self.ask("channel-member-add", {"team": "t", "channel": "town", "user": "carol"})
        self.press()
        self.assertIn(carol, self.c.members[self.pub])
        self.ask("channel-member-remove", {"team": "t", "channel": "secret", "user": "bob"})
        self.assertIn("(private)", self.confirm_post()["message"])
        self.press()
        self.assertNotIn(self.bob, self.c.members[self.priv])
        out = self.adm.command({"user_id": self.req_bot, "token": TOK}, ["rollback", self.confirm_post()["id"]])
        self.assertIn("ask a member of that channel", out, "never back into a private channel")

    def test_bot_create_and_enable(self):
        self.ask("bot-create", {"team": "t", "username": "ann-new", "owner": "ann", "channels": ["town"]})
        self.assertIn("No token is issued", self.confirm_post()["message"])
        self.press()
        bot = next(b for b in self.c.bots.values() if b["username"] == "ann-new")
        self.assertEqual(bot["owner_id"], self.ann)
        self.assertIn(bot["user_id"], self.c.members[self.pub])
        old = self.c.user("ann-old", bot=True, owner=self.ann, deleted=True)
        t1, t2 = self.c.token(old), self.c.token(old)
        self.ask("bot-enable", {"username": "ann-old"})
        self.assertIn("**Live tokens: 2", self.confirm_post()["message"])
        self.press()
        self.assertEqual((self.c.bots[old]["delete_at"], self.c.tokens[t1]["is_active"], self.c.tokens[t2]["is_active"]),
                         (0, False, False), "tokens revoked before enabling")

    def test_failure_midway_keeps_the_rollback_data(self):
        self.ask("bot-create", {"team": "t", "username": "ann-x", "owner": "ann"})
        real = self.c.__call__

        def broken(method, path, body=None):
            if "/assign/" in path:
                raise OSError("network")
            return real(method, path, body)
        self.e._call = broken
        r = self.press()
        self.assertIn("❌ failure midway", r["update"]["message"])
        res = r["update"]["props"][P("admin_result")]
        undo = json.loads((self.tmp / "state" / "undo" / res["backup"]).read_text())
        self.assertTrue(undo["bot"])
        self.assertEqual(self.journal()[-1]["result"], "error")


class Refusals(World):
    def refused(self, out):
        self.assertIn("refused", out)
        self.assertFalse([p for p in self.c.posts.values() if (p["props"] or {}).get(P("admin_confirm"))],
                         "no confirmation post")

    def test_closed_list_and_form(self):
        self.refused(self.ask("role-grant", {"user": "bob"})[1])
        self.assertIn("is not in the list", self.ask("role-grant", {})[1])
        self.assertIn("one line", self.ask("posts-delete", {"post_ids": ["a" * 26]}, why="x\ny")[1])
        self.assertIn("is asked only by ann", self.ask("posts-delete", {"post_ids": ["a" * 26]}, origin="eve")[1])

    def test_dm_private_channels_and_foreign_bots(self):
        p = self.c.post(self.dm, self.bob, "x")
        self.assertIn("DM or a group message", self.ask("posts-delete", {"post_ids": [p]})[1])
        self.c.user("carol")
        self.assertIn("ask a member of that channel",
                      self.ask("channel-member-add", {"team": "t", "channel": "secret", "user": "carol"})[1])
        self.assertIn("ask a member of that channel",
                      self.ask("bot-create", {"team": "t", "username": "z", "owner": "ann", "channels": ["secret"]})[1])
        self.c.user("bob-bot", bot=True, owner=self.bob, deleted=True)
        self.assertIn("not enabling someone else's bot", self.ask("bot-enable", {"username": "bob-bot"})[1])
        self.assertIn("is taken", self.ask("bot-create", {"team": "t", "username": "bob", "owner": "ann"})[1])

    def test_who_calls_and_where(self):
        self.assertIn("only by the admin role's bot", self.ask("posts-delete", {"post_ids": ["a" * 26]}, by=self.ann)[1])
        self.assertIn("only in the admin log channel",
                      self.ask("posts-delete", {"post_ids": ["a" * 26]}, channel=self.pub)[1])
        pid = self.c.post(self.log, self.req_bot, "r", props={P("admin"): {}})
        self.assertIn("token does not match", self.adm.command({"user_id": self.req_bot, "token": "x"}, [pid]))
        self.assertEqual(self.journal()[-1]["why"], "command token")

    def test_audit_posts_are_not_deleted(self):
        p = self.c.post(self.log, self.ann, "audit")
        self.assertIn("the audit is not deleted", self.ask("posts-delete", {"post_ids": [p]})[1])

    def test_press_checks(self):
        p = self.c.post(self.pub, self.bob, "x")
        self.ask("posts-delete", {"post_ids": [p]})
        conf = self.confirm_post()
        ctx = next(a["integration"]["context"] for a in conf["props"]["attachments"][0]["actions"]
                   if a["integration"]["context"]["answer"] == "yes")
        self.assertIn("only a confirmer", self.adm.press({"user_id": self.bob, "post_id": conf["id"], "context": ctx})
                      ["ephemeral_text"])
        forged = dict(ctx)
        forged["sig"] = executor.hmac.new(self.env["EDGE_SECRET"].encode(), b"x", "sha256").hexdigest()
        self.assertIn("signature", self.adm.press({"user_id": self.ann, "post_id": conf["id"], "context": forged})
                      ["ephemeral_text"])
        other = self.c.post(self.log, self.c.me, "elsewhere")
        self.assertIn("not on its own confirmation post",
                      self.adm.press({"user_id": self.ann, "post_id": other, "context": ctx})["ephemeral_text"])
        self.c.post(self.pub, self.ann, "a reply after the dry run", root=p)   # the targets changed
        self.assertIn("fingerprint does not match", self.press()["ephemeral_text"])
        self.assertIn(p, self.c.posts)
        self.assertIn("already pressed", self.press()["ephemeral_text"])

    def test_old_press_and_no(self):
        p = self.c.post(self.pub, self.bob, "x")
        self.ask("posts-delete", {"post_ids": [p]})
        with mock.patch.object(executor.time, "time", return_value=executor.time.time() + 901):
            self.assertIn("15 minutes", self.press()["ephemeral_text"])
        self.ask("posts-delete", {"post_ids": [p]})
        self.assertIn("❌ declined", self.press(answer="no")["update"]["message"])
        self.assertIn(p, self.c.posts)

    def test_rollback_limits(self):
        p = self.c.post(self.pub, self.bob, "x")
        self.ask("posts-delete", {"post_ids": [p]})
        self.press()
        self.assertIn("irreversible", self.adm.command({"user_id": self.req_bot, "token": TOK},
                                                       ["rollback", self.confirm_post()["id"]]))
        self.c.user("dan")
        self.ask("channel-member-add", {"team": "t", "channel": "town", "user": "dan"})
        self.press()
        res = self.c.posts[self.confirm_post()["id"]]
        res["props"][P("admin_result")]["at"] -= 25 * 3600 * 1000
        self.assertIn("within 24 h", self.adm.command({"user_id": self.req_bot, "token": TOK}, ["rollback", res["id"]]))

    def test_secret_must_be_its_own(self):
        self.make_edge(EDGE_ADMIN_SECRET=self.env["EDGE_SECRET"])
        self.assertIn("equals another edge key", self.ask("posts-delete", {"post_ids": ["a" * 26]})[1])
        self.make_edge(EDGE_ADMIN_SECRET="")
        self.assertIn("no EDGE_ADMIN_SECRET", self.ask("posts-delete", {"post_ids": ["a" * 26]})[1])

    def test_api_paths(self):
        self.assertTrue(executor.path_ok("DELETE", f"/posts/{'a' * 26}?permanent=true", "posts-delete"))
        self.assertFalse(executor.path_ok("DELETE", f"/posts/{'a' * 26}?permanent=true", "sidebar-category"))
        self.assertFalse(executor.path_ok("DELETE", f"/channels/{'a' * 26}/members/{'b' * 26}?permanent=true",
                                          "channel-member-remove"))
        self.assertFalse(executor.path_ok("PUT", f"/users/{'a' * 26}/roles", "posts-delete"))
        self.assertFalse(executor.path_ok("DELETE", f"/posts/{'a' * 26}?permanent=false", "posts-delete"),
                         "permanent only as true")
        self.assertFalse(executor.path_ok("POST", "/bots", None), "writes outside an action")
        self.assertTrue(executor.path_ok("POST", "/posts", None), "the edge's own posts")

    def test_origins_setting(self):
        o = executor.parse_origins("posts-delete=ann|steward;*=ann", "ann")
        self.assertEqual(o["bot-create"], ("ann",))
        o = executor.parse_origins("*=ann;posts-delete=ann|steward", "ann")
        self.assertEqual(o["posts-delete"], ("ann", "steward"))
        self.assertEqual(executor.parse_origins("", "")["posts-delete"], (), "no owner, no setting — nobody")


class Requester(IsolatedTest):
    def test_form_and_send(self):
        with self.assertRaises(request.RequestError):
            request.normalize({"action": "x"})
        req = request.normalize({"action": "posts-delete", "target": {"post_ids": ["b" * 26, "b" * 26]}, "why": " w ",
                                 "origin": "ann"})
        self.assertEqual(req["target"], {"post_ids": ["b" * 26]})
        calls = []

        def api(conn, method, path, body=None, token=None):
            calls.append((method, path, body, token))
            return {"id": "p" * 26} if path == "/posts" else {"id": "i" * 26, "text": "admin: dry run ready"}
        with mock.patch.object(request.connections, "bot", return_value="T"), mock.patch.object(request.mattermost, "api", api):
            out = request.send({"NAME": "c", "TEAM": "t", "ADMIN_CHANNEL": "log"}, req)
        self.assertIn("dry run ready", out)
        self.assertEqual(calls[2][2]["props"][P("admin")], req)
        self.assertEqual(calls[3][2]["command"], f"/admin {'p' * 26}")
        self.assertTrue(all(c[3] == "T" for c in calls), "the admin role's bot token")

    def test_cli_dry_and_actions(self):
        r = self.aimost("admin", "actions")
        self.assertIn("bot-enable", r.stdout)
        spec = self.tmp / "r.json"
        spec.write_text(json.dumps({"action": "bot-enable", "target": {"username": "x", "owner": "eve"}, "why": "w",
                                    "origin": "ann"}))
        r = self.aimost("admin", "request", "--spec", str(spec), "--dry")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["target"], {"username": "x"}, "the owner is never taken from a request")
        spec.write_text(json.dumps({"action": "nope"}))
        self.assertEqual(self.aimost("admin", "request", "--spec", str(spec), "--dry").returncode, 2)

    def test_bot_add_without_an_admin_token_asks(self):
        cfg = self.tmp / "config" / "connections"
        cfg.mkdir(parents=True)
        (cfg / "c.env").write_text("URL=http://x\nTEAM=t\nOWNER=ann\n")
        from aimost import cli_bot
        sent = []
        with mock.patch.object(request, "send", lambda conn, req: sent.append(req) or "admin: request x"):
            self.assertEqual(cli_bot.main(["add", "dev", "--desc", "builds", "--via", "c"]), 0)
            self.assertEqual(cli_bot.main(["add", "dev", "--enable", "--via", "c"]), 0)
            self.assertEqual(cli_bot.main(["add", "dev", "--enable", "--owner", "bob", "--via", "c"]), 2)
        self.assertEqual([(r["action"], r["target"]["username"]) for r in sent], [("bot-create", "ann-dev"),
                                                                                  ("bot-enable", "ann-dev")])
        self.assertEqual(sent[0]["target"]["owner"], "ann")
        os.environ["AIMOST_NO_RESTART"] = "1"
        self.addCleanup(os.environ.pop, "AIMOST_NO_RESTART", None)
        tok = self.tmp / "config" / "bots" / "c" / "dev"
        self.assertEqual(cli_bot.main(["add", "dev", "--admit", "--via", "c"]), 3, "no token yet")
        tok.parent.mkdir(parents=True)
        tok.write_text("t")
        os.chmod(tok, 0o644)
        self.assertEqual(cli_bot.main(["add", "dev", "--admit", "--via", "c"]), 3, "readable by others")
        self.assertNotIn("SESSIONS", (cfg / "c.env").read_text(), "not admitted")


class Switch(IsolatedTest):
    def test_off_unless_listed(self):
        self.assertNotIn("/admin", self.aimost("commands", "list").stdout)
        r = self.aimost("commands", "list", env={"AIMOST_PLUGINS": "admin"})
        self.assertIn("/admin <request post id>", r.stdout)
        r = self.aimost("commands", "list", env={"AIMOST_PLUGINS": "admin", "AIMOST_ADMIN_COMMAND": "chat-admin"})
        self.assertIn("/chat-admin", r.stdout, "the command's name is a setting")

    def test_the_edge_routes_presses_to_the_plugin(self):
        from aimost import plugins
        saved = dict(edgemod.POST_ROUTES)
        self.addCleanup(lambda: (edgemod.POST_ROUTES.clear(), edgemod.POST_ROUTES.update(saved)))
        reg = plugins.Registrar(plugins.Plugin("admin", "built-in (optional)", True, optional=True))
        os.environ["AIMOST_PLUGINS"] = "admin"
        self.addCleanup(os.environ.pop, "AIMOST_PLUGINS", None)
        reg.edge_route(executor.BUTTON_PATH, lambda e, b: (200, {"ephemeral_text": "routed"}))
        reg.apply()
        e = edgemod.Edge(env={"EDGE_SECRET": "x"}, call=lambda *a: {}, log=lambda m: None)
        self.assertEqual(e.handle(executor.BUTTON_PATH, b"{}"), (200, {"ephemeral_text": "routed"}))


if __name__ == "__main__":
    unittest.main()
