"""The bridge (delivery, status, commands specs): routing rules offline; the pass against the real disposable server."""

from __future__ import annotations

import os
import types
import time
import unittest
import uuid

from helpers import IsolatedTest
from itenv import IT, requires_mattermost

from lmost import askstore, commands, heartbeat, inbox, link, paths
from lmost import mattermost as mm


# ── offline: routes and the rules a pass applies ───────────────────────────────────────────────────────────────────────
class Routes(unittest.TestCase):
    def test_default_set(self):
        rules = link.parse_routes(link.default_routes({"CHANNEL_OWNERS": "ops:deployer, review:reviewer"}))
        self.assertEqual([r["line"] for r in rules],
                         ["dm -> self", "mention -> self", "top ops -> deployer", "top review -> reviewer",
                          "thread -> self"])

    def test_errors_name_file_and_line(self):
        for text, why in (("dm -> deployer", "only for the bot's own role"), ("top ops", "no “->”"),
                          ("mention @x ops -> self", "a role name, not self"), ("listen ops -> a", "unknown condition"),
                          ("mention @x -> a", "mention @<login>"), ("top -> a", "top <channel>")):
            with self.assertRaises(link.RoutesError) as e:
                link.parse_routes("# comment\n\n" + text, "f.routes")
            self.assertIn("f.routes:3:", str(e.exception))
            self.assertIn(why, str(e.exception))

    def test_mentions(self):
        self.assertEqual(link.mentions({"message": "@ann, @bob.x and a@mail.org @here"}), {"ann", "bob.x"})


def fake_link(c=None, threads=None, humans=None):
    """A Link without a server: one role ``deployer`` (bot id BOT), owner OWN."""
    x = link.Link.__new__(link.Link)
    x.c = {"NAME": "t", "OWNER": "own", **(c or {})}
    x.name, x.relay = "t", ""
    x.bots, x.ids, x.handle = {"deployer": "tok"}, {"deployer": "BOT"}, {"deployer": "me-deployer"}
    x.owner = "OWN"
    x.routes = link.parse_routes(link.default_routes(x.c))
    x.threads = dict(threads or {})
    x.users = {"OWN": {"username": "own"}, "H1": {"username": "h1"}, "H2": {"username": "h2"}, "B9": {"is_bot": True}}
    x.is_human = dict(humans or {})
    x.ctx = types.SimpleNamespace(owner_ids=lambda role: {"OWN"})
    x.paused, x.loop_free = set(), set()
    x.sent = []
    x.api = lambda method, path, body=None, who=None: x.sent.append((method, path, body)) or {}
    return x


class Rules(IsolatedTest):
    OWN = [{"id": "R", "user_id": "BOT", "message": "release at 15:45", "create_at": 1}]
    FOREIGN = [{"id": "R", "user_id": "H2", "message": "memes", "create_at": 1},
               {"id": "a", "user_id": "H1", "message": "@me-deployer what do you think?", "create_at": 2},
               {"id": "b", "user_id": "BOT", "message": "I think so", "create_at": 3}]
    CH = {"name": "random", "type": "O"}

    def rel(self, msg, posts=None, user="H1", root="R", ch=None, c=None, humans=None):
        x = fake_link(c, {"R": posts or self.OWN}, humans)
        return x.relevant({"id": "P", "message": msg, "root_id": root, "user_id": user, "channel_id": "C"}, "deployer",
                          ch or self.CH)

    def test_own_thread_in_full_foreign_only_by_mention(self):
        self.assertEqual(self.rel("and also", user="H2"), "thread")
        self.assertIsNone(self.rel("ok, got it", self.FOREIGN), "the one who called, without @ — no")
        self.assertIsNone(self.rel("lol", self.FOREIGN, user="H2"))
        self.assertEqual(self.rel("@me-deployer you too", self.FOREIGN, user="H2"), "mention")

    def test_dm_and_mention(self):
        self.assertEqual(self.rel("hi", root="", ch={"name": "a__b", "type": "D"}), "dm")
        self.assertEqual(self.rel("@me-deployer hi", root="", ch=self.CH), "mention")
        self.assertIsNone(self.rel("@me-deployers hi", root="", ch=self.CH), "a similar name is not a mention")

    def test_owned_channel_top_level(self):
        c = {"CHANNEL_OWNERS": "ops:deployer"}
        ops = {"name": "ops", "type": "O"}
        self.assertEqual(self.rel("anyone?", root="", ch=ops, c=c), "channel")
        self.assertEqual(self.rel("@own can you look?", root="", ch=ops, c=c), "channel", "only the owner mentioned")
        self.assertIsNone(self.rel("@h2 lunch?", root="", ch=ops, c=c, humans={"h2": True}),
                          "people talking to each other")
        self.assertEqual(self.rel("@h2 and @me-deployer", root="", ch=ops, c=c, humans={"h2": True}), "mention")
        self.assertIsNone(self.rel("anyone?", root="", ch=self.CH, c=c), "another channel")
        self.assertIsNone(self.rel("status", root="", ch=ops, c=c, user="BOT"), "the role's own bot")

    def test_representative_rule_from_file(self):
        x = fake_link()
        x.routes = link.parse_routes("mention @owner review -> deployer\nmention @h2 * -> deployer\n")
        top = {"id": "P", "message": "@own please look", "root_id": "", "user_id": "H1"}
        self.assertEqual(x.relevant(top, "deployer", {"name": "review", "type": "O"}), "representative")
        self.assertIsNone(x.relevant(top, "deployer", {"name": "other", "type": "O"}))
        self.assertIsNone(x.relevant(dict(top, root_id="R"), "deployer", {"name": "review", "type": "O"}),
                          "not in threads")

    def test_mute_and_follow(self):
        box = inbox.Inbox("deployer")
        box.subscribe("mute", "r" * 26)
        own = [dict(self.OWN[0], id="r" * 26)]
        x = fake_link(threads={"r" * 26: own})
        p = {"id": "P", "message": "and also", "root_id": "r" * 26, "user_id": "H2"}
        self.assertIsNone(x.relevant(p, "deployer", self.CH))
        self.assertEqual(x.relevant(dict(p, message="@me-deployer ping"), "deployer", self.CH), "mention")
        box.subscribe("follow", "f" * 26)
        foreign = [dict(self.FOREIGN[0], id="f" * 26)]
        x = fake_link(threads={"f" * 26: foreign})
        self.assertEqual(x.relevant(dict(p, root_id="f" * 26), "deployer", self.CH), "thread (follow)")

    def test_loop_guard(self):
        posts = [{"id": "R", "user_id": "H1", "create_at": 0}] + \
                [{"id": f"p{i}", "user_id": "BOT", "create_at": i} for i in range(1, 8)]
        x = fake_link(threads={"R": posts})
        p = {"id": "p7", "root_id": "R", "create_at": 7, "channel_id": "C"}
        self.assertTrue(x.loop_guard(p, "deployer", self.CH))
        self.assertTrue(x.loop_guard(p, "deployer", self.CH))
        self.assertEqual(len([s for s in x.sent if "waiting for a person" in (s[2] or {}).get("message", "")]), 1,
                         "said once per thread")
        self.assertFalse(x.loop_guard(dict(p, id="p6", create_at=6), "deployer", self.CH), "six in a row — fine")
        x.loop_free = {"agents"}
        self.assertFalse(x.loop_guard(p, "deployer", {"name": "agents", "type": "O"}))
        posts[3]["props"] = {"lmost_answers": {"1": {"user_id": "OWN"}}}
        x = fake_link(threads={"R": posts})
        self.assertFalse(x.loop_guard(p, "deployer", self.CH), "the owner's button press is a person taking part")

    def test_unrouted_waits_for_a_full_pass(self):
        x = fake_link()
        x.delivered, x.full_end = {}, 0.0
        x.unrouted_path = self.tmp / "u.json"
        x.delivered_path = self.tmp / "d.json"
        x.unrouted = {"P": {"p": {"id": "P", "message": "anyone?", "channel_id": "C"}, "hit": False, "passes": 0,
                            "who": "deployer", "expect": True, "at": 10.0}}
        x.warn_unrouted()
        self.assertEqual(x.sent, [], "no verdict on the pass that saw it")
        x.warn_unrouted()
        self.assertEqual(x.sent, [], "no verdict before a full pass that ended later")
        x.full_end = 20.0
        x.warn_unrouted()
        self.assertEqual([s[2]["emoji_name"] for s in x.sent], ["warning"])
        x.unrouted = {"Q": dict(x.unrouted.get("Q") or {"p": {"id": "Q", "channel_id": "C"}, "hit": False, "passes": 1,
                                                          "who": "deployer", "expect": True, "at": 10.0})}
        x.delivered = {"Q": time.time()}
        x.warn_unrouted()
        self.assertEqual(len(x.sent), 1, "delivered by another bot — not a miss")

    def test_stale_command_answer(self):
        x = fake_link()
        x.commands_path = self.tmp / "c.json"
        cp = {"id": "CP", "channel_id": "D", "props": {"lmost_command": {"caller": {"id": "H1"}}}}
        ran = []
        x.run_command = lambda p, who, payload: ran.append(p["id"])
        x.api = lambda method, path, body=None, who=None: x.sent.append((method, path, body)) or \
            (cp if path == "/posts/CP" else {})

        def press(uid, option):
            x.on_command_answer({"id": "Q", "props": {"lmost_question": "ask-1", "lmost_answers": {
                "1": {"user_id": uid, "option": option}}}}, "deployer")
        x.commands = {"done": [], "asked": {"ask-1": "CP"}}
        press("OWN", 1)
        self.assertEqual((ran, x.commands["asked"]), ([], {"ask-1": "CP"}), "a non-caller's press does not count")
        press("H1", 1)
        self.assertEqual(ran, ["CP"])
        x.commands = {"done": [], "asked": {"ask-1": "CP"}}
        press("H1", 2)
        self.assertEqual((ran, x.commands["done"]), (["CP"], ["CP"]), "no — dropped, not run")
        self.assertIn("Dropped.", [(b or {}).get("message") for _, _, b in x.sent])

    def test_command_key(self):
        x = fake_link({"EDGE_SECRET": "s", "EDGE_KEY_ID": "k1"})
        self.assertEqual(x.command_key({"kid": "k1"}), "s")
        self.assertEqual(x.command_key({}), "s")
        self.assertIsNone(x.command_key({"kid": "k2"}))
        self.assertIsNone(fake_link().command_key({}))


class Restart(IsolatedTest):
    def test_unreachable_server_stays_pending(self):
        (self.tmp / "config" / "connections").mkdir(parents=True)
        (self.tmp / "config" / "bots" / "down").mkdir(parents=True)
        (self.tmp / "config" / "connections" / "down.env").write_text("URL=http://127.0.0.1:9\nOWNER=x\nSESSIONS=r1\n")
        (self.tmp / "config" / "bots" / "down" / "r1").write_text("tok")
        os.environ["LMOST_API_TIMEOUT"] = "2"
        self.addCleanup(os.environ.pop, "LMOST_API_TIMEOUT", None)
        pending = {"down": {"NAME": "down", "URL": "http://127.0.0.1:9", "OWNER": "x", "SESSIONS": "r1"}}
        self.assertEqual(link.connect(pending), [])
        self.assertIn("down", pending, "kept for a retry, not dropped")

    def test_broken_routes_stop_the_bridge(self):
        (self.tmp / "config" / "connections").mkdir(parents=True)
        (self.tmp / "config" / "connections" / "a.env").write_text("URL=http://127.0.0.1:9\nOWNER=x\n")
        (self.tmp / "config" / "connections" / "a.routes").write_text("dm -> self\nbogus\n")
        r = self.lmost("link", "once")
        self.assertEqual(r.returncode, 2)
        self.assertIn("a.routes:2:", r.stderr)


# ── against the real server ────────────────────────────────────────────────────────────────────────────────────────────
def owner_api(method, path, body=None, token=None):
    return mm.api({"NAME": "it", "URL": IT["LMOST_IT_URL"]}, method, path, body,
                  token=token or IT["LMOST_IT_TOKEN_OWNER"])


@requires_mattermost
class Real(IsolatedTest):
    """Fresh bots and a channel per class; a fresh config and state per test."""

    @classmethod
    def setUpClass(cls):
        tag = uuid.uuid4().hex[:6]
        cls.made, cls.tok, cls.uid = [], {}, {}
        team = IT["LMOST_IT_TEAM_ID"]
        for role in ("alpha", "beta", "rly"):
            name = f"l{tag}-{role}"
            b = owner_api("POST", "/bots", {"username": name, "display_name": name})
            cls.made.append(b["user_id"])
            cls.uid[role] = b["user_id"]
            cls.tok[role] = owner_api("POST", f"/users/{b['user_id']}/tokens", {"description": "it"})["token"]
            owner_api("POST", f"/teams/{team}/members", {"team_id": team, "user_id": b["user_id"]})
        cls.handle = {r: f"l{tag}-{r}" for r in cls.tok}
        cls.chname = "link-" + tag
        cls.ch = owner_api("POST", "/channels", {"team_id": team, "name": cls.chname, "display_name": cls.chname,
                                                 "type": "O"})["id"]
        for uid in (IT["LMOST_IT_USER_ID_COLLEAGUE"], cls.uid["alpha"], cls.uid["beta"]):
            owner_api("POST", f"/channels/{cls.ch}/members", {"user_id": uid})

    @classmethod
    def tearDownClass(cls):
        for uid in cls.made:
            owner_api("POST", f"/bots/{uid}/disable")

    def setUp(self):
        super().setUp()
        cfg = self.tmp / "config"
        (cfg / "connections").mkdir(parents=True)
        (cfg / "bots" / "it").mkdir(parents=True)
        for r, t in self.tok.items():
            (cfg / "bots" / "it" / r).write_text(t)
        self.env = cfg / "connections" / "it.env"
        self.write_env()
        os.environ["LMOST_ACTIVITY_SYNC"] = "1"
        self.addCleanup(os.environ.pop, "LMOST_ACTIVITY_SYNC", None)

    def write_env(self, extra=""):
        self.env.write_text(f"URL={IT['LMOST_IT_URL']}\nTEAM={IT['LMOST_IT_TEAM']}\nOWNER=owner\n"
                            f"SESSIONS=alpha,beta,probe\nRELAY=rly\nEDGE_PUBLIC={IT.get('LMOST_IT_EDGE_PUBLIC', '')}\n"
                            f"EDGE_SECRET={IT.get('LMOST_IT_EDGE_SECRET', '')}\n" + extra)

    def start(self):
        """A Link with its first pass done: the boundary is now, later posts are new."""
        x = link.Link({**__import__("lmost.connections", fromlist=["x"]).connections()["it"]})
        x.once()
        time.sleep(0.05)
        return x

    def dm(self, role, token=None):
        return owner_api("POST", "/channels/direct", [self.uid[role], IT["LMOST_IT_USER_ID_OWNER"]])["id"]

    def post(self, channel, message, token=None, root="", props=None):
        body = {"channel_id": channel, "message": message}
        if root:
            body["root_id"] = root
        if props:
            body["props"] = props
        return owner_api("POST", "/posts", body, token=token)

    def events(self, role):
        return [e for _, e in inbox.Inbox(role).unread()]

    def reactions(self, pid, role):
        return sorted(r["emoji_name"] for r in owner_api("GET", f"/posts/{pid}/reactions") or []
                      if r["user_id"] == self.uid[role])

    def test_dm_mention_thread_and_receipt(self):
        x = self.start()
        p = self.post(self.dm("alpha"), "deploy please")
        m = self.post(self.ch, f"@{self.handle['beta']} look", token=IT["LMOST_IT_TOKEN_COLLEAGUE"])
        root = owner_api("POST", "/posts", {"channel_id": self.ch, "message": "release notes"}, token=self.tok["alpha"])
        r = self.post(self.ch, "a question", token=IT["LMOST_IT_TOKEN_COLLEAGUE"], root=root["id"])
        x.once()
        a = self.events("alpha")
        self.assertEqual([(e["kind"], e["role"], e["from"], e["channel"], e["post"]) for e in a],
                         [("message", "owner", "owner", "dm", p["id"]), ("message", "person", "colleague", self.chname,
                                                                          r["id"])])
        self.assertEqual(a[0]["via"], "it")
        self.assertEqual(a[1]["thread"], root["id"])
        b = self.events("beta")
        self.assertEqual([(e["role"], e["post"]) for e in b], [("person", m["id"])])
        self.assertEqual(self.reactions(p["id"], "alpha"), ["mailbox_with_mail"])
        self.assertEqual(self.reactions(m["id"], "beta"), ["mailbox_with_mail"])
        x.once()
        self.assertEqual(len(self.events("alpha")), 2, "nothing twice")

    def test_cli_mode_receipt_and_read_receipt(self):
        x = self.start()
        inbox.set_mode("alpha", "cli")
        p = self.post(self.dm("alpha"), "when you are back")
        x.once()
        self.assertEqual(self.reactions(p["id"], "alpha"), ["keyboard"])
        inbox.Inbox("alpha").unread(ack=True)
        self.assertIn("eyes", self.reactions(p["id"], "alpha"), "👀 once read")

    def test_two_bots_one_post(self):
        """The first bot's 📬 moves update_at; the second bot still gets the post as new."""
        x = self.start()
        p = self.post(self.ch, f"@{self.handle['alpha']} @{self.handle['beta']} both of you",
                      token=IT["LMOST_IT_TOKEN_COLLEAGUE"])
        x.once({"alpha"})
        x.once({"beta"})
        self.assertEqual([e["post"] for e in self.events("alpha")], [p["id"]])
        self.assertEqual([e["post"] for e in self.events("beta")], [p["id"]])

    def test_owner_reaction_is_an_event(self):
        x = self.start()
        mine = owner_api("POST", "/posts", {"channel_id": self.dm("alpha"), "message": "merge it?"},
                         token=self.tok["alpha"])
        x.once()
        owner_api("POST", "/reactions", {"user_id": IT["LMOST_IT_USER_ID_OWNER"], "post_id": mine["id"],
                                         "emoji_name": "+1"})
        x.once()
        x.once()
        ev = self.events("alpha")
        self.assertEqual([(e["kind"], e["role"]) for e in ev], [("reaction", "owner")])
        self.assertIn(":+1: — yes / agreed", ev[0]["text"])

    def test_typo_gets_a_warning(self):
        x = self.start()
        p = self.post(self.dm("alpha"), "@nobody-" + uuid.uuid4().hex[:5] + " hi")
        x.once()     # delivered to alpha (a DM) — no warning
        q = self.post(self.ch, "@nobody-" + uuid.uuid4().hex[:5] + " are you there?")
        for _ in range(3):
            x.once()
        self.assertEqual(self.reactions(q["id"], "alpha") + self.reactions(q["id"], "beta"), ["warning"])
        th = owner_api("GET", f"/posts/{q['id']}/thread")
        self.assertTrue(any("not delivered" in th["posts"][i]["message"] for i in th["order"]))
        self.assertNotIn("warning", self.reactions(p["id"], "alpha"))
        r = self.post(self.ch, "people talking")
        for _ in range(3):
            x.once()
        self.assertEqual(self.reactions(r["id"], "alpha") + self.reactions(r["id"], "beta"), [],
                         "a channel without an owner role — people's talk, no warning")

    def test_foreign_bot_only_by_mention_as_peer(self):
        x = self.start()
        dep = IT["LMOST_IT_BOT_DEPLOYER"]   # a bot of the test world that is not a role here
        owner_api("POST", f"/channels/{self.ch}/members", {"user_id": IT["LMOST_IT_BOT_ID_DEPLOYER"]})
        self.post(self.ch, "build failed", token=dep)
        m = self.post(self.ch, f"@{self.handle['alpha']} checked", token=dep)
        x.once()
        self.assertEqual([(e["role"], e["post"]) for e in self.events("alpha")], [("peer", m["id"])])

    def test_relay(self):
        x = self.start()
        dm = owner_api("POST", "/channels/direct", [self.uid["rly"], IT["LMOST_IT_USER_ID_OWNER"]])["id"]
        root = owner_api("POST", "/posts", {"channel_id": dm, "message": "from session probe · via relay\nhi",
                                            "props": {"agent": "probe", "relay": "rly"}}, token=self.tok["rly"])
        x.once()
        r = self.post(dm, "answer to probe", root=root["id"])
        out = self.post(dm, "to whom?")
        x.once()
        self.assertEqual([(e["role"], e["post"], e["thread"]) for e in self.events("probe")],
                         [("owner", r["id"], root["id"])])
        th = owner_api("GET", f"/posts/{out['id']}/thread")
        self.assertTrue(any("does not know which session" in th["posts"][i]["message"] for i in th["order"]))
        plain = owner_api("POST", "/posts", {"channel_id": dm, "message": "no relay mark", "props": {"agent": "probe"}},
                          token=self.tok["rly"])
        self.post(dm, "reply", root=plain["id"])
        x.once()
        self.assertEqual(len(self.events("probe")), 1, "a root without the relay mark is not a session's thread")

    def test_heartbeat(self):
        x = self.start()
        inbox.Inbox("alpha").put("x", sender="s")
        x.heartbeat(force=True)
        hb = heartbeat.fetch(lambda m, p: owner_api(m, p), self.uid["alpha"])
        self.assertEqual((hb["role"], hb["state"], hb["unread"]), ("alpha", "idle", 1))
        self.assertLess(heartbeat.age_s(hb), 30)
        self.assertIsNone(heartbeat.fetch(lambda m, p: owner_api(m, p), self.uid["rly"]), "the relay is not a role")

    # ── answers and command posts need the edge of the test world ──────────────────────────────────────────────────
    def need_edge(self):
        if not IT.get("LMOST_IT_EDGE_URL"):
            self.skipTest("no edge in the test world")

    def press(self, pid, action, token=None):
        owner_api("POST", f"/posts/{pid}/actions/{action}", {}, token=token)

    def test_button_answer_reaches_the_asker(self):
        self.need_edge()
        from lmost import post as postmod
        x = self.start()
        postmod.Poster("alpha", via="it", warn=lambda m: None).ask(
            {"parts": [{"title": "deploy now?", "text": "", "options": ["yes", "no"]}]})
        q = askstore.open_asks("alpha")[-1]
        try:
            self.press(q["post"], "p1o2", token=IT["LMOST_IT_TOKEN_COLLEAGUE"])   # not an addressee: the edge refuses
        except mm.HTTPError:
            pass
        self.press(q["post"], "p1o1")
        x.once()
        x.once()
        ev = [e for e in self.events("alpha") if e["kind"] == "answer"]
        self.assertEqual(len(ev), 1, ev)
        self.assertEqual((ev[0]["role"], ev[0]["from"]), ("owner", "owner"))
        self.assertIn(f"answer to {q['q']} item 1 «deploy now?»: yes", ev[0]["text"])
        self.assertEqual(askstore.asks("alpha")[q["q"]]["state"], "answered")

    def command(self, role, sub, args, at=None, secret=None, root="", caller=None):
        payload = {"name": "agent", "sub": sub, "args": args,
                   "caller": caller or {"id": IT["LMOST_IT_USER_ID_OWNER"], "username": "owner"}, "bot": self.uid[role],
                   "team_id": IT["LMOST_IT_TEAM_ID"], "channel_id": self.ch, "root_id": root,
                   "at": int((at or time.time()) * 1000)}
        payload["sig"] = commands.sign(payload, secret or IT["LMOST_IT_EDGE_SECRET"])
        return owner_api("POST", "/posts", {"channel_id": self.dm(role), "message": f"`/agent {sub}`",
                                            "props": {"lmost_command": payload}})

    def replies(self, pid):
        th = owner_api("GET", f"/posts/{pid}/thread")
        return [th["posts"][i]["message"] for i in th["order"] if i != pid]

    def test_session_command_becomes_an_event(self):
        self.need_edge()
        x = self.start()
        good = self.command("alpha", "ask", ["alpha", "status of the release?"])
        bad = self.command("alpha", "ask", ["alpha", "forged"], secret="wrong")
        x.once()
        ev = self.events("alpha")
        self.assertEqual([(e["kind"], e["role"], e["text"], e["post"]) for e in ev],
                         [("command", "owner", "/agent ask status of the release?", good["id"])])
        self.assertEqual(self.reactions(good["id"], "alpha"), ["mailbox_with_mail"])
        self.assertEqual(self.reactions(bad["id"], "alpha"), [])
        x.once()
        self.assertEqual(len(self.events("alpha")), 1, "once")

    def test_command_closed_to_the_caller(self):
        self.need_edge()
        x = self.start()
        cp = self.command("alpha", "mute", ["alpha"], root="t" * 26,
                          caller={"id": IT["LMOST_IT_USER_ID_COLLEAGUE"], "username": "colleague"})
        x.once()
        self.assertTrue(any("is not open to you" in m for m in self.replies(cp["id"])))
        self.assertFalse((paths.inbox_dir() / "alpha.mute").exists())

    def test_tick_hooks_keep_the_pin(self):
        self.write_env("WAITING_POST=alpha\n")
        x = self.start()
        pins = owner_api("GET", f"/channels/{self.dm('alpha')}/pinned")
        self.assertTrue(any((pins["posts"][i].get("props") or {}).get("lmost_waiting") for i in pins["order"]),
                        "waiting.tick runs after a pass")

    def test_mute_command_in_a_thread(self):
        self.need_edge()
        x = self.start()
        root = "t" * 26
        cp = self.command("alpha", "mute", ["alpha"], root=root)
        x.once()
        self.assertIn(root, (paths.inbox_dir() / "alpha.mute").read_text())
        self.assertTrue(any("muted" in m for m in self.replies(cp["id"])))
        other = self.command("alpha", "mute", ["beta"], root=root)
        x.once()
        self.assertTrue(any("is not the bot of role beta" in m for m in self.replies(other["id"])))

    def test_stale_command_asks_the_caller(self):
        self.need_edge()
        x = self.start()
        cp = self.command("alpha", "ask", ["alpha", "old news"], at=time.time() - 3600)
        x.once()
        self.assertEqual(self.events("alpha"), [], "not run silently")
        th = owner_api("GET", f"/posts/{cp['id']}/thread")
        q = next(th["posts"][i] for i in th["order"] if (th["posts"][i].get("props") or {}).get("lmost_question"))
        self.assertIn("min old", q["message"])
        self.press(q["id"], "p1o1")
        x.once()
        x.once()
        ev = self.events("alpha")
        self.assertEqual([(e["kind"], e["text"]) for e in ev], [("command", "/agent ask old news")])


if __name__ == "__main__":
    unittest.main()
