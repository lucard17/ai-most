"""Question registry, snoozes, discussions — shared storage (questions spec)."""

from __future__ import annotations

import json
import unittest

from helpers import IsolatedTest

from lmost import askstore, paths


class Registry(IsolatedTest):
    def test_records_fold_by_ask_id(self):
        askstore.ask_record("r", {"q": "ask-1", "post": "p1", "title": "deploy?", "state": "open"})
        askstore.ask_record("r", {"q": "ask-2", "post": "p2", "state": "open"})
        askstore.ask_record("r", {"q": "ask-1", "state": "cancelled", "why": "moot"})
        a = askstore.asks("r")
        self.assertEqual(list(a), ["ask-1", "ask-2"])
        self.assertEqual(a["ask-1"], {"q": "ask-1", "post": "p1", "title": "deploy?", "state": "cancelled", "why": "moot"})
        self.assertEqual([r["q"] for r in askstore.open_asks("r")], ["ask-2"])
        self.assertEqual(askstore.ask_roles(), ["r"])

    def test_broken_line_ignored(self):
        askstore.ask_record("r", {"q": "ask-1"})
        with open(askstore.asks_dir() / "r.jsonl", "a") as f:
            f.write("{broken\n")
        self.assertEqual(list(askstore.asks("r")), ["ask-1"])

    def test_ask_state(self):
        spec = {"parts": [{"title": "a"}, {"title": "b"}]}
        self.assertEqual(askstore.ask_state({"props": {"lmost_ask": spec, "lmost_answers": {"0": "x"}}}), "open")
        self.assertEqual(askstore.ask_state({"props": {"lmost_ask": spec, "lmost_answers": {"0": "x", "1": "y"}}}),
                         "answered")
        self.assertEqual(askstore.ask_state({"props": {"lmost_cancelled": {"why": "w"}}}), "cancelled")


class Snoozes(IsolatedTest):
    def test_expired_snooze_is_gone(self):
        p = askstore.snooze_path("work")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"items": {"ask-1": {"until": 1000}, "ask-2": {"until": None}, "ask-3": {"until": 3000}}}))
        self.assertEqual(sorted(askstore.snoozes("work", now_ms=2000)), ["ask-2", "ask-3"])
        self.assertEqual(askstore.snoozes("none"), {})


if __name__ == "__main__":
    unittest.main()
