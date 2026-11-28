"""Question upkeep on the bridge side (questions spec): answers from the edge's marks, "Waiting for you", reminders,
snoozes, "not relevant", discussions, deadlines, backfill, answer sinks.

Real presses go through the disposable Mattermost and the test world's edge (`tests/integration/edge-up.sh`); marks the
edge cannot be made to write (a free-text dialog, a forged author) are written straight into props.
"""

from __future__ import annotations

import json
import os
import time
import unittest
import uuid

from helpers import IsolatedTest
from itenv import IT, requires_mattermost

from aimost import askstore, inbox, plugins, questions, waiting
from aimost import mattermost as mm
from aimost import post as postmod

def _edge_up() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(IT["AIMOST_IT_EDGE_URL"] + "/version", timeout=2):
            return True
    except Exception:
        return False


requires_edge = unittest.skipUnless(IT.get("AIMOST_IT_EDGE_URL") and _edge_up(), "the test world's edge is not running")


class Offline(IsolatedTest):
    def test_age(self):
        self.assertEqual(waiting.age(0, 59 * 60000), "59 min")
        self.assertEqual(waiting.age(0, 3 * 3600000), "3 h")
        self.assertEqual(waiting.age(0, 72 * 3600000), "3 d")

    def test_pin_sections_and_unique_action_ids(self):
        class C:
            c = {"OWNER": "boss", "EDGE_PUBLIC": "edge:1", "EDGE_SECRET": "s"}
        now = 10 * 3600000
        rows = [{"blocking": True, "at": 0, "who": "a", "title": "deploy?", "link": "L1", "q": "ask-1", "asker": "a",
                 "key": "ask-1", "section": waiting.CHAT_SECTION},
                {"blocking": False, "at": 0, "who": "tracker", "title": "T-1 done?", "link": "", "key": "tracker:T-1",
                 "section": "tracker"},
                {"blocking": False, "at": 1, "who": "b", "title": "merge?", "link": "L2", "q": "ask-2", "asker": "b",
                 "key": "ask-2", "section": waiting.CHAT_SECTION}]
        later = [{"who": "c", "title": "old", "key": "ask-9"}]
        msg, atts = waiting.render_pin(C, rows, later, now)
        self.assertTrue(msg.startswith("📋 Waiting for you — 3\n**Chat questions — 2**\n- 🔴 a · deploy? · waiting 10 h"))
        self.assertIn("**tracker — 1**\n- tracker · T-1 done?", msg)
        self.assertIn("💤 snoozed: 1", msg)
        ids = [a["id"] for att in atts for a in att["actions"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([a["name"] for a in atts[1]["actions"]], ["snooze…"], "a sink line has no 'not relevant'")
        back = atts[-1]["actions"][0]
        self.assertEqual(back["options"][0]["value"], "back:ask-9")
        ctx = atts[0]["actions"][0]["integration"]["context"]
        self.assertEqual((ctx["kind"], ctx["q"], ctx["to"], ctx["secret"]), ("waiting-drop", "ask-1", ["boss"], "s"))
        self.assertEqual(waiting.render_pin(C, [], [], now)[0].splitlines()[0], "📋 Waiting for you — nothing ✅")


class Sink:
    def __init__(self, refuse=False):
        self.calls, self.refuse = [], refuse

    def answer(self, conn, q, part, a, title, ref=None):
        self.calls.append((q, part, a.get("option"), title) + ((ref,) if ref is not None else ()))
        if self.refuse:
            raise waiting.SinkRefused("item is closed")
        return "recorded"

    def rows(self, conn):
        return [{"key": "T-7", "title": "close T-7?", "at": 1}]


@requires_mattermost
class Real(IsolatedTest):
    def setUp(self):
        super().setUp()
        os.environ["AIMOST_WAITING_EVERY_S"] = "0"
        self.addCleanup(os.environ.pop, "AIMOST_WAITING_EVERY_S", None)
        self.addCleanup(waiting._ctx.clear)
        saved = dict(plugins.ANSWER_SINKS)       # tests register under the built-in's name, which is on everywhere
        self.addCleanup(lambda: (plugins.ANSWER_SINKS.clear(), plugins.ANSWER_SINKS.update(saved)))
        self.conn0 = {"NAME": "it", "URL": IT["AIMOST_IT_URL"]}
        u = uuid.uuid4().hex[:6]
        self.asker, self.pinner = "wa" + u, "wp" + u
        self.bot_ids, self.toks = {}, {}
        cfg = self.tmp / "config"
        (cfg / "bots" / "it").mkdir(parents=True)
        (cfg / "connections").mkdir(parents=True)
        for role in (self.asker, self.pinner):
            b = self.owner("POST", "/bots", {"username": role, "display_name": role})
            self.bot_ids[role] = b["user_id"]
            self.toks[role] = self.owner("POST", f"/users/{b['user_id']}/tokens", {"description": "it"})["token"]
            self.owner("POST", f"/teams/{IT['AIMOST_IT_TEAM_ID']}/members",
                       {"team_id": IT["AIMOST_IT_TEAM_ID"], "user_id": b["user_id"]})
            (cfg / "bots" / "it" / role).write_text(self.toks[role])
        self.env_file = cfg / "connections" / "it.env"
        self.write_env()

    def tearDown(self):
        for bid in self.bot_ids.values():
            try:
                self.owner("POST", f"/bots/{bid}/disable")
            except mm.HTTPError:
                pass
        super().tearDown()

    def write_env(self, extra="", pin=True, digest=""):
        self.env_file.write_text(
            f"URL={IT['AIMOST_IT_URL']}\nTEAM={IT['AIMOST_IT_TEAM']}\nOWNER=owner\nSESSIONS={self.asker},{self.pinner}\n"
            + (f"WAITING_POST={self.pinner}\n" if pin else "") + f"WAITING_DIGEST_AT={digest}\nEDGE_PUBLIC={IT.get('AIMOST_IT_EDGE_PUBLIC', 'edge:1')}\n"
            f"EDGE_SECRET={IT.get('AIMOST_IT_EDGE_SECRET', 's')}\n" + extra)
        waiting._ctx.clear()

    @property
    def conn(self):
        from aimost import connections
        return connections.connections()["it"]

    def owner(self, method, path, body=None):
        return mm.api(self.conn0, method, path, body, token=IT["AIMOST_IT_TOKEN_OWNER"])

    def as_bot(self, method, path, body=None, role=None):
        return mm.api(self.conn0, method, path, body, token=self.toks[role or self.asker])

    def ask(self, title="deploy now?", channel="dm", to="", blocking=False, ttl=None, sink="", ref=""):
        out = postmod.Poster(self.asker, via="it", warn=lambda m: None).ask(
            {"context": "", "parts": [{"title": title, "text": "", "options": ["yes", "no"]}]},
            channel=channel, to=to, blocking=blocking, ttl=ttl, sink=sink, ref=ref)
        pid = out[0].split("post ")[1].rstrip(")")
        q = out[1].split()[1]
        return q, pid

    def post(self, pid):
        return self.owner("GET", f"/posts/{pid}")

    def mark(self, pid, key, value):
        """Write a mark the way the edge would — the post's author (the asking bot) patches its props."""
        p = self.post(pid)
        props = dict(p["props"])
        props[key] = value
        self.as_bot("PUT", f"/posts/{pid}/patch", {"props": props})
        return self.post(pid)

    def dm_id(self):
        return self.as_bot("POST", "/channels/direct", [self.bot_ids[self.asker], IT["AIMOST_IT_USER_ID_OWNER"]])["id"]

    def events(self, role=None):
        return [e for _, e in inbox.Inbox(role or self.asker).unread(ack=True)]

    def pin(self):
        st = json.loads((self.tmp / "state" / "link" / "it.waiting.json").read_text())
        return self.post(st["post"])

    # ── answers ───────────────────────────────────────────────────────────────────────────────────────────────────
    @requires_edge
    def test_button_answer_reaches_the_session_once(self):
        q, pid = self.ask()
        self.owner("POST", f"/posts/{pid}/actions/p1o1", {})
        p = self.post(pid)
        waiting.on_post(self.conn, p)
        waiting.on_post(self.conn, p)
        ev = self.events()
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["kind"], ev[0]["role"], ev[0]["from"], ev[0]["via"]), ("answer", "owner", "owner", "it"))
        self.assertEqual(ev[0]["text"], f"answer to {q} item 1 «deploy now?»: yes")
        self.assertEqual(askstore.asks(self.asker)[q]["state"], "answered")

    def test_free_text_answer_carries_the_retell_demand(self):
        q, pid = self.ask()
        p = self.mark(pid, "aimost_answers", {"1": {"text": "only after 18:00", "by": "owner",
                                                   "user_id": IT["AIMOST_IT_USER_ID_OWNER"], "at": int(time.time() * 1000)}})
        waiting.on_post(self.conn, p)
        ev = self.events()
        self.assertIn("only after 18:00", ev[0]["text"])
        self.assertIn(inbox.FREE_TEXT_MARK, ev[0]["text"])

    def test_who_may_answer(self):
        q, pid = self.ask()
        at = int(time.time() * 1000)
        p = self.mark(pid, "aimost_answers", {"1": {"option": 1, "by": "colleague",
                                                   "user_id": IT["AIMOST_IT_USER_ID_COLLEAGUE"], "at": at}})
        waiting.on_post(self.conn, p)
        self.assertEqual(self.events(), [], "not an addressee")
        # the same marks on someone else's post (a forged author) are ignored
        forged = self.owner("POST", "/posts", {"channel_id": p["channel_id"], "message": "x", "props": {
            "aimost_question": q, "agent": self.asker, "aimost_ask": p["props"]["aimost_ask"],
            "aimost_answers": {"1": {"option": 1, "by": "owner", "user_id": IT["AIMOST_IT_USER_ID_OWNER"], "at": at}}}})
        waiting.on_post(self.conn, forged)
        self.assertEqual(self.events(), [])
        # a co-owner of the role counts
        self.write_env(f"CO_OWNERS={self.asker}:colleague\n")
        waiting.on_post(self.conn, self.post(pid))
        self.assertEqual([e["role"] for e in self.events()], ["owner"])

    def test_answer_after_cancel_does_not_count(self):
        q, pid = self.ask()
        postmod.Poster(self.asker, via="it", warn=lambda m: None).cancel(q, "moot")
        p = self.mark(pid, "aimost_answers", {"1": {"option": 1, "by": "owner", "user_id": IT["AIMOST_IT_USER_ID_OWNER"],
                                                   "at": int(time.time() * 1000) + 1000}})
        waiting.on_post(self.conn, p)
        self.assertEqual(self.events(), [])

    # ── the pin ───────────────────────────────────────────────────────────────────────────────────────────────────
    @requires_edge
    def test_pin_drop_and_snooze(self):
        q1, p1 = self.ask("merge?")
        q2, p2 = self.ask("deploy?", blocking=True)
        now = time.time()
        waiting.tick(self.conn, now)
        pin = self.pin()
        self.assertTrue(pin.get("is_pinned"))
        lines = pin["message"].splitlines()
        self.assertEqual(lines[0], "📋 Waiting for you — 2")
        self.assertEqual(lines[1], "**Chat questions — 2**")
        self.assertTrue(lines[2].startswith(f"- 🔴 {self.asker} · deploy?"), lines)
        # "not relevant" on the 🔴 line (row 0), pressed by the owner through the edge
        self.owner("POST", f"/posts/{pin['id']}/actions/drop0", {})
        waiting.on_post(self.conn, self.post(pin["id"]))     # the bridge sees the pin's edit
        waiting.tick(self.conn, now + 1)
        self.assertTrue(self.post(p2)["message"].startswith("⏹ Question cancelled: dropped by @owner from Waiting for you"))
        self.assertEqual(askstore.asks(self.asker)[q2]["state"], "cancelled")
        ev = self.events()
        self.assertEqual([e["kind"] for e in ev], ["cancelled"])
        pin = self.pin()
        self.assertTrue(pin["message"].startswith("📋 Waiting for you — 1"))
        self.assertNotIn("aimost_waiting_drop", pin["props"], "the redraw cleared the processed mark")
        # snooze the remaining line for a day
        self.owner("POST", f"/posts/{pin['id']}/actions/snooze0", {"selected_option": "1d"})
        waiting.on_post(self.conn, self.post(pin["id"]))
        waiting.tick(self.conn, now + 2)
        pin = self.pin()
        self.assertTrue(pin["message"].startswith("📋 Waiting for you — nothing ✅"), pin["message"])
        self.assertIn("💤 snoozed: 1", pin["message"])
        self.assertEqual([e["kind"] for e in self.events()], ["snoozed"])
        self.assertIn(q1, askstore.snoozes("it"))
        # a day later the line is back
        waiting.tick(self.conn, now + 86400 + 60)
        self.assertTrue(self.pin()["message"].startswith("📋 Waiting for you — 1"))

    def test_snooze_by_someone_else_and_back(self):
        q, pid = self.ask()
        waiting.tick(self.conn)
        pin = self.pin()
        ctx = waiting.context(self.conn)
        at = int(time.time() * 1000)
        pin["props"]["aimost_waiting_snooze"] = {q: {"value": "forever", "asker": self.asker,
                                                    "user_id": IT["AIMOST_IT_USER_ID_COLLEAGUE"], "at": at}}
        waiting.on_waiting_snooze(ctx, pin, None)
        self.assertEqual(askstore.snoozes("it"), {}, "only the owner snoozes")
        pin["props"]["aimost_waiting_snooze"] = {q: {"value": "forever", "asker": self.asker,
                                                    "user_id": IT["AIMOST_IT_USER_ID_OWNER"], "at": at}}
        waiting.on_waiting_snooze(ctx, pin, None)
        waiting.on_waiting_snooze(ctx, pin, None)
        self.assertIsNone(askstore.snoozes("it", now_ms=at + 40 * 86400000)[q]["until"])
        self.assertEqual(len(self.events()), 1, "a mark is applied once")
        pin["props"]["aimost_waiting_snooze"] = {"": {"value": "back:" + q, "user_id": IT["AIMOST_IT_USER_ID_OWNER"],
                                                     "at": at + 5}}
        waiting.on_waiting_snooze(ctx, pin, None)
        self.assertEqual(askstore.snoozes("it"), {})

    def test_redraw_does_not_lose_a_mark(self):
        """A press the bridge has not handled yet is applied before the redraw: a patch replaces props whole."""
        q1, _ = self.ask("first?")
        now = time.time()
        waiting.tick(self.conn, now)
        pin = self.pin()
        props = dict(pin["props"], aimost_waiting_snooze={q1: {"value": "7d", "asker": self.asker,
                                                              "user_id": IT["AIMOST_IT_USER_ID_OWNER"],
                                                              "at": int(now * 1000)}})
        self.as_bot("PUT", f"/posts/{pin['id']}/patch", {"props": props}, role=self.pinner)
        self.ask("second?")                                  # the contents change — a redraw is due
        waiting.tick(self.conn, now + 1)
        msg = self.pin()["message"]
        self.assertIn("second?", msg)
        self.assertNotIn("first?", msg)
        self.assertIn("💤 snoozed: 1", msg)

    def test_pin_marks_only_on_the_pin(self):
        q, pid = self.ask()
        p = self.mark(pid, "aimost_waiting_drop", {q: {"asker": self.asker, "user_id": IT["AIMOST_IT_USER_ID_OWNER"],
                                                      "at": 1}})
        waiting.on_post(self.conn, p)
        self.assertEqual(askstore.asks(self.asker)[q]["state"], "open")

    def test_off_without_waiting_post(self):
        self.write_env(pin=False)
        self.ask()
        waiting.tick(self.conn)
        self.assertFalse((self.tmp / "state" / "link" / "it.waiting.json").exists())

    def test_rows_blocking_on_top_then_oldest(self):
        self.ask("first?")
        self.ask("second?")
        self.ask("urgent?", blocking=True)
        self.ask("to someone else?", channel="dm:colleague")
        rows = waiting.waiting_rows(waiting.context(self.conn))
        self.assertEqual([r["title"] for r in rows], ["urgent?", "first?", "second?"])

    # ── reminders ─────────────────────────────────────────────────────────────────────────────────────────────────
    def reminders(self):
        dm = self.as_bot("POST", "/channels/direct", [self.bot_ids[self.pinner], IT["AIMOST_IT_USER_ID_OWNER"]],
                         role=self.pinner)["id"]
        r = self.owner("GET", f"/channels/{dm}/posts")
        return [r["posts"][i] for i in r["order"]
                if (r["posts"][i].get("props") or {}).get("aimost_remind") or r["posts"][i]["props"].get("aimost_digest")]

    def test_blocking_question_reminders(self):
        q, _ = self.ask("deploy?", blocking=True)
        self.ask("lunch?")
        t0 = time.time()
        waiting.waiting_remind(waiting.context(self.conn), t0 + 30 * 60)
        self.assertEqual(self.reminders(), [], "before an hour — nothing")
        waiting.waiting_remind(waiting.context(self.conn), t0 + 65 * 60)
        waiting.waiting_remind(waiting.context(self.conn), t0 + 120 * 60)
        rem = self.reminders()
        self.assertEqual(len(rem), 1)
        self.assertIn("⏰ Waiting for an answer, work stops without it: 🔴", rem[0]["message"])
        self.assertEqual(rem[0]["props"]["aimost_remind"], q)
        waiting.waiting_remind(waiting.context(self.conn), t0 + 190 * 60)
        self.assertEqual(len(self.reminders()), 2)

    def test_digest_once_per_slot_and_none_when_empty(self):
        slot = time.strftime("%H:%M", time.localtime(time.time() - 60))
        self.write_env(digest=slot)
        waiting.waiting_remind(waiting.context(self.conn))
        self.assertEqual(self.reminders(), [], "nothing waiting — no digest")
        (self.tmp / "state" / "link" / "it.remind.json").unlink()
        self.ask("a?")
        self.ask("b?", blocking=True)
        waiting.waiting_remind(waiting.context(self.conn))
        waiting.waiting_remind(waiting.context(self.conn))
        rem = self.reminders()
        self.assertEqual(len(rem), 1)
        self.assertTrue(rem[0]["message"].startswith("📋 Digest: 2 waiting, 1 of them 🔴"), rem[0]["message"])

    # ── deadlines and backfill ────────────────────────────────────────────────────────────────────────────────────
    def test_ttl_cancels_at_the_deadline(self):
        q, pid = self.ask(ttl=1)
        ctx = waiting.context(self.conn)
        waiting.expire_asks(ctx, time.time() * 1000 + 30 * 60000)
        self.assertEqual(askstore.asks(self.asker)[q]["state"], "open")
        waiting.expire_asks(ctx, time.time() * 1000 + 61 * 60000)
        self.assertTrue(self.post(pid)["message"].startswith("⏹ Question cancelled: deadline 1 h passed"))
        self.assertEqual(askstore.asks(self.asker)[q]["state"], "cancelled")

    def test_backfill(self):
        q, pid = self.ask("asked long ago?")
        (askstore.asks_dir() / f"{self.asker}.jsonl").unlink()
        self.assertEqual(waiting.backfill(self.conn, days=1), 1)
        rec = askstore.asks(self.asker)[q]
        self.assertEqual((rec["post"], rec["to"], rec["state"], rec["title"]), (pid, ["owner"], "open", "asked long ago?"))
        self.assertEqual(waiting.backfill(self.conn, days=1), 0)
        self.assertEqual(askstore.snoozes("it"), {})
        (askstore.asks_dir() / f"{self.asker}.jsonl").unlink()
        self.write_env("BACKFILL_SNOOZE_DAYS=0\n")
        time.sleep(0.01)
        self.assertEqual(waiting.backfill(self.conn, days=1), 1)
        self.assertIn(q, askstore.snoozes("it"), "older than the threshold — snoozed until I say")

    # ── discussions ───────────────────────────────────────────────────────────────────────────────────────────────
    def test_discussion_in_a_dm(self):
        name = "wd-" + uuid.uuid4().hex[:8]
        ch = self.owner("POST", "/channels", {"team_id": IT["AIMOST_IT_TEAM_ID"], "name": name, "display_name": name,
                                              "type": "O"})
        for uid in (self.bot_ids[self.asker], IT["AIMOST_IT_USER_ID_COLLEAGUE"], IT["AIMOST_IT_USER_ID_OUTSIDER"]):
            self.owner("POST", f"/channels/{ch['id']}/members", {"user_id": uid})
        q, pid = self.ask("which region?", channel=name, to="colleague")
        # the marks the edge writes on a press (the press itself — tests/test_edge*.py)
        p = self.mark(pid, "aimost_discuss", {IT["AIMOST_IT_USER_ID_COLLEAGUE"]: {"at": 1, "by": "colleague"},
                                             IT["AIMOST_IT_USER_ID_OUTSIDER"]: {"at": 1, "by": "outsider"}})
        waiting.on_post(self.conn, p)
        waiting.on_post(self.conn, p)
        d = askstore.discussions("it")
        self.assertEqual([v["user"] for v in d.values()], ["colleague"], "one thread, not for a non-addressee")
        root = next(iter(d))
        self.assertTrue(self.post(root)["message"].startswith("💬 Discussion: which region? — [original post]("))
        th = self.owner("GET", f"/posts/{pid}/thread")
        self.assertIn("💬 discussion moved to DM with @colleague", [x["message"] for x in th["posts"].values()])
        ev = self.events()
        self.assertEqual((ev[0]["kind"], ev[0]["thread"], ev[0]["role"]), ("discuss", root, "person"))
        self.assertIn(f"--conclude {root}", ev[0]["text"])
        self.assertTrue(any(a.get("actions") and a["actions"][0]["id"] == "p1o1" for a in self.post(pid)["props"]["attachments"]),
                        "answer buttons stay live")

    # ── answer sinks ──────────────────────────────────────────────────────────────────────────────────────────────
    def test_sink_gets_only_the_owners_answer(self):
        sink = Sink()
        plugins.ANSWER_SINKS["tracker"] = ("gitlab", sink)
        self.write_env(f"CO_OWNERS={self.asker}:colleague\n")
        q, pid = self.ask("close T-1?", sink="tracker")
        at = int(time.time() * 1000)
        p = self.mark(pid, "aimost_answers", {"1": {"option": 1, "by": "colleague",
                                                   "user_id": IT["AIMOST_IT_USER_ID_COLLEAGUE"], "at": at}})
        waiting.on_post(self.conn, p)
        self.assertEqual((sink.calls, self.events()), ([], []), "a co-owner's answer does not reach a sink")
        p = self.mark(pid, "aimost_answers", {"1": {"option": 2, "by": "owner",
                                                   "user_id": IT["AIMOST_IT_USER_ID_OWNER"], "at": at + 1}})
        waiting.on_post(self.conn, p)
        self.assertEqual(sink.calls, [(q, 1, 2, "close T-1?")])
        self.assertEqual(self.events()[0]["text"], f"answer to {q} item 1 «close T-1?» → tracker: recorded")
        # the sink's own open questions get their own section
        rows = waiting.waiting_rows(waiting.context(self.conn))
        self.assertIn(("tracker", "tracker:T-7"), [(r["section"], r["key"]) for r in rows])

    def test_sink_gets_its_own_question_id(self):
        sink = Sink()
        plugins.ANSWER_SINKS["tracker"] = ("gitlab", sink)
        q, pid = self.ask("close T-3?", sink="tracker", ref="T-3")
        p = self.mark(pid, "aimost_answers", {"1": {"option": 1, "by": "owner", "user_id": IT["AIMOST_IT_USER_ID_OWNER"],
                                                   "at": int(time.time() * 1000)}})
        waiting.on_post(self.conn, p)
        self.assertEqual(sink.calls, [(q, 1, 1, "close T-3?", "T-3")])
        with self.assertRaises(postmod.PostError):
            self.ask("x?", sink="tracker", ref="two words")

    def test_a_question_an_earlier_version_posted_is_answered_through_the_sink_that_claims_it(self):
        sink = Sink()
        sink.claims = lambda q: q.startswith("T-")
        plugins.ANSWER_SINKS["tracker"] = ("gitlab", sink)
        spec = {"context": "", "parts": [{"title": "close T-4?", "text": "", "options": ["yes", "no"]}]}
        p = self.as_bot("POST", "/posts", {"channel_id": self.dm_id(), "message": "close T-4?",
                                           "props": {"aimost_question": "T-4", "aimost_spec": spec, "agent": self.asker}})
        at = int(time.time() * 1000)
        p = self.mark(p["id"], "aimost_answers", {"1": {"option": 2, "by": "colleague",
                                                       "user_id": IT["AIMOST_IT_USER_ID_COLLEAGUE"], "at": at}})
        waiting.on_post(self.conn, p)
        self.assertEqual(sink.calls, [], "only the owner answers a sink question")
        p = self.mark(p["id"], "aimost_answers", {"1": {"option": 2, "by": "owner", "user_id": IT["AIMOST_IT_USER_ID_OWNER"],
                                                       "at": at + 1}})
        waiting.on_post(self.conn, p)
        self.assertEqual(sink.calls, [("T-4", 1, 2, "close T-4?", "T-4")])
        self.assertEqual(self.events()[0]["text"], "answer to T-4 item 1 «close T-4?» → tracker: recorded")
        del sink.claims
        p = self.mark(p["id"], "aimost_answers", {"1": {"option": 1, "by": "owner", "user_id": IT["AIMOST_IT_USER_ID_OWNER"],
                                                       "at": at + 2}})
        waiting.on_post(self.conn, p)
        self.assertEqual(len(sink.calls), 1, "nobody claims it — not answered")

    def test_sink_supplies_its_buttons_and_its_plugin_marks_the_answer_done(self):
        sink = Sink()
        url = "/plugins/tracker/actions/answer"
        seen = []
        sink.buttons = lambda spec, n, conn=None: seen.append(conn["NAME"]) or [{"name": o, "integration": {"url": url, "context": {
            "v": 1, "q": spec["ref"], "part": n, "option": m}}} for m, o in enumerate(spec["parts"][n - 1]["options"], 1)]
        plugins.ANSWER_SINKS["tracker"] = ("gitlab", sink)
        q, pid = self.ask("close T-9?", sink="tracker", ref="T-9")
        props = self.post(pid)["props"]
        self.assertEqual([(a["id"], a["name"]) for a in props["attachments"][0]["actions"]], [("p1s1", "yes"), ("p1s2", "no")],
                         "the sink's buttons only — no edge buttons, no own-answer button")
        self.assertEqual(seen, ["it"], "the sink gets the connection")
        acts = props["aimost_ask"]["parts"][0]["actions"]    # the server hides integration from readers; the spec keeps it
        self.assertEqual([(a["id"], a["name"], a["integration"]["url"], a["integration"]["context"]) for a in acts],
                         [("p1s1", "yes", url, {"v": 1, "q": "T-9", "part": 1, "option": 1}),
                          ("p1s2", "no", url, {"v": 1, "q": "T-9", "part": 1, "option": 2})],
                         "the sink's buttons only — no edge buttons, no own-answer button")
        # the sink's plugin took the press of a colleague (its side checks rights) and merged the mark into the props
        p = self.mark(pid, "aimost_answers", {"1": {"option": 2, "by": "colleague", "done": "tracker",
                                                   "user_id": IT["AIMOST_IT_USER_ID_COLLEAGUE"],
                                                   "at": int(time.time() * 1000)}})
        waiting.on_post(self.conn, p)
        self.assertEqual(sink.calls, [], "a done mark is not passed to the sink again")
        self.assertEqual([e["text"] for e in self.events()],
                         [f"answer to {q} item 1 «close T-9?» → tracker: no, recorded by tracker"])
        p = self.post(pid)
        self.assertTrue(p["message"].startswith("✅"), p["message"])
        self.assertNotIn("actions", p["props"]["attachments"][0], "drawn again: the answer instead of the buttons")
        self.assertEqual((p["props"]["aimost_question"], p["props"]["agent"]), (q, self.asker), "props kept")
        waiting.on_post(self.conn, p)
        self.assertEqual(self.events(), [], "once")
        self.assertEqual(askstore.asks(self.asker)[q]["state"], "answered")

    def test_a_sink_with_broken_buttons_refuses_the_question(self):
        sink = Sink()
        sink.buttons = lambda spec, n: [{"name": "yes"}]
        plugins.ANSWER_SINKS["tracker"] = ("gitlab", sink)
        with self.assertRaises(questions.AskError):
            self.ask("close T-10?", sink="tracker", ref="T-10")

    def test_sink_refusal_is_posted(self):
        plugins.ANSWER_SINKS["tracker"] = ("gitlab", Sink(refuse=True))
        q, pid = self.ask("close T-2?", sink="tracker")
        p = self.mark(pid, "aimost_answers", {"1": {"option": 1, "by": "owner", "user_id": IT["AIMOST_IT_USER_ID_OWNER"],
                                                   "at": int(time.time() * 1000)}})
        waiting.on_post(self.conn, p)
        self.assertEqual(self.events(), [])
        th = self.owner("GET", f"/posts/{pid}/thread")
        self.assertIn("⚠️ tracker refused: item is closed", [x["message"] for x in th["posts"].values()])
