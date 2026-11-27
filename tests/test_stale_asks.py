"""Stop reminder to cancel stale questions (questions spec). Oracle: upstream chat-stale-asks.test.sh, part (1);
the "Waiting for you" pin part belongs to the bridge."""

from __future__ import annotations

import json
import time
import unittest

from fakes import SessionTest

from lmost import askstore, paths, stale_asks


class Reminder(SessionTest):
    def setUp(self):
        super().setUp()
        cfg = paths.config_dir() / "connections"
        cfg.mkdir(parents=True)
        (cfg / "it.env").write_text("URL=http://127.0.0.1:1\nOWNER=owner1\nTEAM=t\nSESSIONS=probe\n")
        self.sid = self.session("probe")
        self.tp = str(self.transcript(self.sid))
        at = int((time.time() - 7200) * 1000)
        askstore.ask_record("probe", {"q": "ask-1", "post": "Q1", "via": "it", "at": at,
                                      "title": "Ship at  14:00?", "to": ["owner1"], "state": "open"})

    def turn(self, kind, uid):
        self.append(self.sid, self.user(kind, uid=uid), {"type": "assistant", "message": {"content": "ok"}})

    def test_owner_words_in_terminal(self):
        self.turn("human", "u1")
        r = stale_asks.reminder("probe", self.tp)
        self.assertIn("- ask-1 · waiting 2 h · «Ship at 14:00?»", r)
        self.assertIn("lmost post --cancel", r)
        self.assertIsNone(stale_asks.reminder("probe", self.tp), "second Stop of the same turn — silent")

    def test_peer_turn_silent_unless_owner_words_mark(self):
        self.turn("peer", "u2")
        self.assertIsNone(stale_asks.reminder("probe", self.tp))
        self.turn("peer", "u3")
        paths.private_dir(paths.marks_dir())
        (paths.marks_dir() / "probe.owner-words").write_text("1")
        self.assertIsNotNone(stale_asks.reminder("probe", self.tp), "the owner answered in words from chat")

    def test_owner_typed_mid_turn(self):
        self.turn("peer", "u4")
        self.append(self.sid, self.queued("human"))
        self.assertIsNotNone(stale_asks.reminder("probe", self.tp))

    def test_cancelled_foreign_and_snoozed_left_out(self):
        askstore.ask_record("probe", {"q": "ask-1", "state": "cancelled"})
        askstore.ask_record("probe", {"q": "ask-2", "via": "it", "to": ["colleague"], "state": "open", "title": "x"})
        self.turn("human", "u5")
        self.assertIsNone(stale_asks.reminder("probe", self.tp))
        askstore.ask_record("probe", {"q": "ask-3", "via": "it", "to": ["owner1"], "state": "open", "title": "y"})
        p = askstore.snooze_path("it")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"items": {"ask-3": {"until": None}}}))
        self.turn("human", "u6")
        self.assertIsNone(stale_asks.reminder("probe", self.tp), "snoozed by the owner — not reminded")

    def test_ages(self):
        self.assertEqual([stale_asks._age(m) for m in (5, 125, 3000)], ["5 min", "2 h", "2 d"])


if __name__ == "__main__":
    unittest.main()
