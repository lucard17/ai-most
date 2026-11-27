"""Who started the turn, did it end — from the transcript (authority: peer mark; delivery: dispatcher "busy").
Oracle: upstream chat-peer-guard.test.sh (turn_origin part), chat-dispatch-turn.test.sh."""

from __future__ import annotations

import json
import unittest

from fakes import SessionTest

from lmost import turn_origin as to


class Origin(SessionTest):
    def kind(self, *recs) -> str:
        sid = self.session("s")
        self.append(sid, *recs)
        return to.origin(str(self.transcript(sid)))[0]

    def test_typed_input_is_human(self):
        self.assertEqual(self.kind(self.user("human", "check the tabs")), "human")

    def test_pasted_wake_line_is_still_human(self):
        self.assertEqual(self.kind(self.user("human", "lmost dispatch → s: 1 new in inbox — /inbox")), "human")

    def test_inbox_command_is_wake(self):
        self.assertEqual(self.kind(self.user("human", "/inbox")), "wake")
        self.assertEqual(self.kind(self.user(
            "human", "<command-message>inbox</command-message>\n<command-name>/inbox</command-name>")), "wake")

    def test_dispatcher_message_is_wake_only_at_start(self):
        msg = ('Another Claude session sent a message:\n<cross-session-message from="uds:/x.sock" from-name="t">\n'
               "lmost dispatch → s: 1 new in inbox — /inbox\n</cross-session-message>")
        self.assertEqual(self.kind(self.user("peer", msg)), "wake")
        mid = msg.replace("lmost dispatch → s", "do X; see lmost dispatch → log")
        self.assertEqual(self.kind(self.user("peer", mid)), "peer")

    def test_last_turn_start_wins_tool_results_skipped(self):
        tool_result = {"type": "user", "message": {"content": [{"type": "tool_result"}]}}
        self.assertEqual(self.kind(self.user("peer"), self.user("human"), tool_result), "human")

    def test_no_transcript(self):
        self.assertEqual(to.origin(str(self.tmp / "none.jsonl")), ("none", 0))
        self.assertEqual(self.kind(), "none")

    def test_start_time(self):
        sid = self.session("s")
        self.append(sid, self.user("human", offset_s=-100))
        k, t = to.origin(str(self.transcript(sid)))
        self.assertAlmostEqual(t, __import__("time").time() - 100, delta=3)


class OwnerTyped(SessionTest):
    def test_queued_human_after_mark(self):
        import time
        sid = self.session("s")
        p = str(self.transcript(sid))
        self.append(sid, self.queued("peer", 5))
        self.assertFalse(to.owner_typed_since(p, time.time()), "another session's message does not count")
        self.append(sid, self.queued("human", -120))
        self.assertFalse(to.owner_typed_since(p, time.time()), "words typed before the mark do not count")
        self.append(sid, self.queued("human", 5))
        self.assertTrue(to.owner_typed_since(p, time.time()))


class TurnEnded(SessionTest):
    A = {"type": "assistant", "message": {"stop_reason": "end_turn"}}
    S1 = {"type": "system", "subtype": "stop_hook_summary"}
    S2 = {"type": "system", "subtype": "turn_duration"}
    Q = {"type": "queue-operation", "operation": "enqueue"}

    def ended(self, *recs) -> bool:
        p = self.tmp / "j.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in recs))
        return to.turn_ended(str(p))

    def test_cases(self):
        self.assertTrue(self.ended(self.A, self.S1, self.S2))
        self.assertTrue(self.ended(self.A, self.S1, self.S2, self.Q, self.Q, {"type": "pr-link"}, {"type": "mode"}),
                        "after the end only background queue records")
        self.assertFalse(self.ended(self.A, self.S2, {"type": "user"}, {"type": "api-request"}), "a new turn started")
        self.assertFalse(self.ended({"type": "user"}, {"type": "assistant"}), "turn running")
        self.assertFalse(self.ended(self.A, self.S2, self.Q, {"type": "queue-operation", "operation": "dequeue"},
                                    {"type": "user"}), "queue handed into a turn")

    def test_broken_tail_does_not_crash(self):
        p = self.tmp / "b.jsonl"
        p.write_bytes(b"\xbe\xff" + json.dumps(self.S2).encode() + b"\n" + json.dumps(self.S2).encode() + b"\n")
        self.assertTrue(to.turn_ended(str(p)))


if __name__ == "__main__":
    unittest.main()
