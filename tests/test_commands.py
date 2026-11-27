"""Slash commands (commands spec): registry, roles, built-in edge commands, command posts — offline; heartbeat (status spec)."""

from __future__ import annotations

import json
import time
import urllib.parse

from fakemm import FakeMM
from helpers import IsolatedTest

from aimost import commands, edgecmds, heartbeat
from aimost import edge as edgemod

H = 3600


class Base(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.mm = FakeMM()
        self.mm.user("UO", "owner")
        self.mm.user("UC", "colleague")
        self.mm.user("BD", "owner-deployer", bot=True, owner="UO")
        self.mm.user("BV", "owner-devops", bot=True, owner="UO")
        self.mm.user("BX", "other-bot", bot=True, owner="UC")
        self.lines = []
        self.e = edgemod.Edge(env={"EDGE_BOT_TOKEN": "t", "EDGE_SECRET": "s3", "EDGE_PUBLIC": "edge:1",
                                   "EDGE_KEYS_FILE": str(self.tmp / "keys.json")}, call=self.mm.call,
                              log=self.lines.append)

    def run_cmd(self, name, user, text, **extra):
        form = {"user_id": user, "user_name": self.mm.users.get(user, {}).get("username", ""), "team_id": "T",
                "channel_id": "CH", "text": text, **extra}
        code, r = self.e.handle(f"/{name}?k={self.e.slash_key(name)}", urllib.parse.urlencode(form).encode())
        self.assertEqual((code, r.get("response_type")), (200, "ephemeral"))
        return r["text"]

    def beat(self, uid, state="idle", unread=0, age_s=10, role=None, last_turn=None):
        now_ms = int((time.time() - age_s) * 1000)
        self.mm.prefs[(uid, heartbeat.CATEGORY)] = {heartbeat.NAME: heartbeat.value(
            role or self.mm.users[uid]["username"], state, unread, last_turn, now_ms=now_ms)}


class Registry(Base):
    def test_declaration_rules(self):
        with self.assertRaises(ValueError):
            commands.Command("x", "", "edge", "h")                          # edge without handler
        with self.assertRaises(ValueError):
            commands.Command("x", "", "bridge", "h", handler=lambda c: "")  # bridge with handler
        with self.assertRaises(ValueError):
            commands.Command("x", "", "nowhere", "h")
        with self.assertRaises(ValueError):
            commands.Command("x", "", "session", "h", who=("admin",))
        with self.assertRaises(ValueError):
            commands.register(commands.Command("agent", "who", "session", "dup"))
        with self.assertRaises(ValueError):
            commands.register(commands.Command("unfollow", "more", "session", "a trigger with own args"))

    def test_v1_commands(self):
        got = {(n, s): c.kind for n in commands.names() for s, c in commands.subcommands(n).items()}
        self.assertEqual(got, {("unfollow", ""): "edge", ("bot-token", ""): "edge", ("agent", "status"): "edge",
                               ("agent", "who"): "edge", ("agent", "waiting"): "bridge", ("agent", "mute"): "bridge",
                               ("agent", "follow"): "bridge", ("agent", "ask"): "session"})

    def test_help_lists_only_what_the_caller_may_use(self):
        self.mm.user("UH", "nobots")
        person = self.run_cmd("agent", "UH", "help")
        self.assertIn("/agent ask", person)
        self.assertNotIn("/agent status", person)
        self.assertNotIn("/agent mute", person)
        owner = self.run_cmd("agent", "UO", "help")
        for sub in ("status", "who", "waiting", "mute", "follow", "ask"):
            self.assertIn(f"/agent {sub}", owner)
        self.assertIn("Unknown subcommand `nope`", self.run_cmd("agent", "UO", "nope"))

    def test_registration_urls_are_keyed(self):
        regs = {r["trigger"]: r for r in commands.registration("edge:1", "sl4sh", "T")}
        self.assertEqual(sorted(regs), ["agent", "bot-token", "unfollow"])
        self.assertEqual(regs["agent"]["url"], f"http://edge:1/agent?k={commands.slash_key('sl4sh', 'agent')}")
        self.assertEqual(regs["agent"]["method"], "P")
        self.assertEqual(self.e.handle("/agent?k=" + commands.slash_key("other", "agent"), b"user_id=UO")[0], 403)

    def test_sign_verify(self):
        p = {"name": "agent", "sub": "mute", "args": ["deployer"], "caller": {"id": "UO"}}
        p["sig"] = commands.sign(p, "k")
        self.assertTrue(commands.verify(p, "k"))
        self.assertFalse(commands.verify(dict(p, args=["devops"]), "k"))
        self.assertFalse(commands.verify(p, "other"))
        self.assertFalse(commands.verify(p, ""))

    def test_parse_quotes(self):
        self.assertEqual(commands.parse('ask deployer "deploy now"'), ["ask", "deployer", "deploy now"])
        self.assertEqual(commands.parse('ask deployer "unbalanced'), ["ask", "deployer", '"unbalanced'])


class Authority(Base):
    def test_role_is_checked_before_the_handler(self):
        self.beat("BX", age_s=5)
        out = self.run_cmd("agent", "UO", "mute other-bot")
        self.assertIn("not open to you (you are person here)", out)
        self.assertEqual(self.mm.requests("POST", "^/posts$"), [], "no command post for a refused caller")
        self.assertIn("not open to you (you are peer here)", self.run_cmd("agent", "BD", "who"))
        self.assertEqual(self.run_cmd("agent", "nobody", "who"), "Unknown caller — refused.")

    def test_colleague_session_command_carries_the_caller(self):
        self.beat("BD", age_s=5)
        out = self.run_cmd("agent", "UC", 'ask owner-deployer "deploy now"')
        self.assertEqual(out, "Sent to @owner-deployer.")
        post = self.mm.posts["new1"]
        cmd = post["props"]["aimost_command"]
        self.assertEqual((cmd["sub"], cmd["args"], cmd["caller"]), ("ask", ["owner-deployer", "deploy now"],
                                                                   {"id": "UC", "username": "colleague"}))
        self.assertEqual(post["channel_id"], "dm-BD-UC", "in the caller's DM with the target bot")
        self.assertTrue(commands.verify(cmd, "s3"), "signed with the bot owner's key")
        self.assertNotIn("role", cmd, "the bridge resolves the sender role itself")

    def test_usage_and_unknown_role(self):
        self.assertEqual(self.run_cmd("agent", "UO", "ask deployer"), 'Usage: `/agent ask <role> "<text>"`')
        self.assertEqual(self.run_cmd("agent", "UO", "mute"), "Usage: `/agent mute <role>`")
        self.assertEqual(self.run_cmd("agent", "UO", "mute nobody"), "No bot for role `nobody`.")


class CommandPosts(Base):
    def test_role_by_prefix_and_owner_key(self):
        self.e.master = "m4ster"
        self.e.keys_file.write_text(json.dumps({"k1": {"owner_id": "UO", "gen": 2}}))
        self.beat("BD", age_s=5)
        self.assertEqual(self.run_cmd("agent", "UO", "mute deployer", root_id="R1"), "Sent to @owner-deployer.")
        cmd = self.mm.posts["new1"]["props"]["aimost_command"]
        self.assertEqual((cmd["kid"], cmd["root_id"], cmd["bot"]), ("k1", "R1", "BD"))
        self.assertTrue(commands.verify(cmd, self.e.key_for("k1", 2)))
        self.assertFalse(commands.verify(cmd, "s3"))

    def test_bridge_offline(self):
        self.beat("BD", age_s=10 * 60)
        out = self.run_cmd("agent", "UO", "follow deployer")
        self.assertTrue(out.startswith("Bridge offline since "), out)
        self.assertIn("will deliver to @owner-deployer when it is back", out)
        self.assertIn("aimost_command", self.mm.posts["new1"]["props"], "the command post stays")
        self.assertIn("never seen", self.run_cmd("agent", "UO", "follow devops"))

    def test_waiting_goes_to_the_freshest_bot(self):
        self.beat("BD", age_s=100)
        self.beat("BV", age_s=5)
        self.assertEqual(self.run_cmd("agent", "UO", "waiting"), "Sent to @owner-devops.")

    def test_no_key_for_that_owner(self):
        self.e.owner_login = "colleague"           # the shared key belongs to another owner
        self.beat("BD", age_s=5)
        self.assertIn("has no button key", self.run_cmd("agent", "UO", "mute deployer"))
        self.assertEqual(self.mm.requests("POST", "^/posts$"), [])


class StatusWho(Base):
    def test_fresh_heartbeat(self):
        lt = int((time.time() - 120) * 1000)
        self.beat("BD", "running", unread=2, age_s=30, role="deployer", last_turn=lt)
        self.beat("BV", "idle", age_s=40, role="devops")
        out = self.run_cmd("agent", "UO", "status deployer")
        self.assertIn("@owner-deployer (deployer) — running · unread 2 · last turn "
                      + time.strftime("%H:%M", time.localtime(lt / 1000)), out)
        self.assertIn("_data 30 s old_", out)
        self.assertNotIn("owner-devops", out)
        who = self.run_cmd("agent", "UO", "who")
        self.assertIn("@owner-devops (devops) — idle · unread 0", who)
        self.assertIn("@owner-deployer", who)
        self.assertNotIn("other-bot", who)
        self.assertEqual(self.run_cmd("agent", "UO", "status"), who)

    def test_machine_off(self):
        at = time.time() - 2 * H
        self.beat("BD", "running", age_s=2 * H)
        self.beat("BV", "idle", age_s=3 * H)
        out = self.run_cmd("agent", "UO", "who")
        self.assertTrue(out.startswith("Bridge offline since " + time.strftime("%H:%M", time.localtime(at))), out)
        self.assertIn("@owner-deployer — running", out)
        self.assertIn("@owner-devops — idle", out)

    def test_offline_after_three_minutes(self):
        self.beat("BD", "idle", age_s=heartbeat.STALE_S - 20)
        self.assertNotIn("offline", self.run_cmd("agent", "UO", "status deployer"))
        self.beat("BD", "idle", age_s=heartbeat.STALE_S + 60)
        self.assertIn("Bridge offline since", self.run_cmd("agent", "UO", "status deployer"))

    def test_not_your_bot_and_no_bots(self):
        self.assertEqual(self.run_cmd("agent", "UC", "status owner-deployer"), "@owner-deployer is not your bot.",
                         "a colleague with own bots is owner only of those")
        self.mm.user("UN", "nobots")
        self.assertIn("not open to you (you are person here)", self.run_cmd("agent", "UN", "who"))
        self.assertIn("no heartbeat yet", self.run_cmd("agent", "UO", "who"))


class Heartbeat(IsolatedTest):
    def test_value_parse_age(self):
        hb = heartbeat.parse(heartbeat.value("deployer", "waiting", 3, 1000, now_ms=5000))
        self.assertEqual(hb, {"seen": 5000, "role": "deployer", "state": "waiting", "unread": 3, "last_turn": 1000})
        self.assertEqual(heartbeat.age_s(hb, now=15), 10)
        self.assertFalse(heartbeat.offline(hb, now=5 + heartbeat.STALE_S))
        self.assertTrue(heartbeat.offline(hb, now=6 + heartbeat.STALE_S))
        self.assertTrue(heartbeat.offline(None))
        for bad in (None, "", "x", "[1]", '{"seen": "x"}', '{"role": "r"}'):
            self.assertIsNone(heartbeat.parse(bad), bad)
        self.assertEqual(heartbeat.parse('{"seen": 1}')["state"], "?")
        with self.assertRaises(ValueError):
            heartbeat.value("r", "sleeping")

    def test_fetch(self):
        mm = FakeMM()
        self.assertIsNone(heartbeat.fetch(mm.call, "B"), "never written")
        mm.prefs[("B", heartbeat.CATEGORY)] = {heartbeat.NAME: heartbeat.value("r", "idle", now_ms=7)}
        self.assertEqual(heartbeat.fetch(mm.call, "B")["seen"], 7)


class Unfollow(Base):
    def setUp(self):
        super().setUp()
        now = time.time() * 1000
        hms = H * 1000

        def t(tid, by, last, mentions=0):
            return {"id": tid, "post": {"user_id": by, "create_at": now - 9 * hms}, "last_reply_at": last,
                    "unread_mentions": mentions}
        self.mm.threads["UC"] = [t("t_old", "BD", now - 3 * hms), t("t_fresh", "BD", now - 1 * hms),
                                 t("t_mention", "BD", now - 5 * hms, 1), t("t_human", "UO", now - 5 * hms),
                                 t("t_other", "BV", now - 5 * hms), t("t_broken", "BD", 0)]
        self.mm.fail[r"threads/t_broken/following"] = 500

    def unfollowed(self):
        return sorted(r[1].split("/")[-2] for r in self.mm.requests("DELETE") if "t_broken" not in r[1])

    def test_commands_and_sweep(self):
        self.assertIn("is not a bot", self.run_cmd("unfollow", "UC", "@owner"))
        self.assertEqual(self.mm.prefs, {})
        self.assertIn("out of bounds", self.run_cmd("unfollow", "UC", "@owner-deployer 10m"))
        self.assertTrue(self.run_cmd("unfollow", "UC", "@owner-deployer 2h").startswith("On:"))
        self.assertEqual(json.loads(self.mm.prefs[("UC", "aimost_unfollow")]["BD"]), {"hours": 2.0})
        self.assertIn("UC", self.mm.prefs[("uEDGE", "aimost_unfollow_users")])
        self.assertIn("@owner-deployer — after 2 h of quiet", self.run_cmd("unfollow", "UC", ""))
        self.assertIn("No unfollows", self.run_cmd("unfollow", "UC", "list"))
        edgecmds.unfollow_sweep(self.e)
        self.assertEqual(self.unfollowed(), ["t_old"])
        self.assertTrue(any("t_broken" in ln for ln in self.lines), "a failing thread is logged, the sweep goes on")
        self.assertIn("@owner-deployer · https://chat.example/team-T/pl/t_old", self.run_cmd("unfollow", "UC", "list"))
        self.assertIn("is off", self.run_cmd("unfollow", "UC", "@owner-deployer off"))
        self.assertNotIn("UC", self.mm.prefs[("uEDGE", "aimost_unfollow_users")])
        self.mm.log.clear()
        edgecmds.unfollow_sweep(self.e)
        self.assertEqual(self.unfollowed(), [])

    def test_hours(self):
        self.assertEqual(edgecmds.parse_hours("30m"), 0.5)
        self.assertEqual(edgecmds.parse_hours("1d"), 24)
        self.assertEqual(edgecmds.parse_hours("1,5"), 1.5)
        self.assertIsNone(edgecmds.parse_hours("soon"))


class BotToken(Base):
    def test_owner_only_and_never_logged(self):
        self.assertIn("only they issue", self.run_cmd("bot-token", "UC", "owner-deployer"))
        self.mm.bots["BV"] = None
        self.assertIn("no human owner", self.run_cmd("bot-token", "UO", "owner-devops"))
        self.assertIn("is not a bot", self.run_cmd("bot-token", "UO", "colleague"))
        out = self.run_cmd("bot-token", "UO", "owner-deployer")
        self.assertIn("```\nTOKEN-VALUE\n```", out)
        self.assertFalse(any("TOKEN-VALUE" in ln for ln in self.lines))
        self.assertTrue(any("id TID" in ln for ln in self.lines))
        self.assertNotIn("EDGE_KEY_ID", out, "no EDGE_MASTER — no key offered")

    def test_bot_callers_and_bot_owners_refused(self):
        self.assertIn("not open to you (you are peer here)", self.run_cmd("bot-token", "BD", "owner-deployer"))
        self.mm.user("BB", "bot-owned-bot", bot=True, owner="BD")
        self.assertIn("no human owner", self.run_cmd("bot-token", "UO", "bot-owned-bot"))

    def test_revoke_walks_all_pages(self):
        self.mm.tokens["BD"] = [f"t{i}" for i in range(205)]
        self.assertIn("revoked: 205", self.run_cmd("bot-token", "UO", "owner-deployer revoke"))
        self.assertEqual(len(self.mm.requests("POST", "/users/tokens/revoke")), 205)
        self.assertTrue(any("revoked 205 of 205" in ln for ln in self.lines))
        self.assertIn("only they issue", self.run_cmd("bot-token", "UC", "owner-deployer revoke"))


if __name__ == "__main__":
    import unittest
    unittest.main()
