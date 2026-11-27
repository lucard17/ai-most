"""Ask spec, question post rendering, cancel patch — offline (spec: questions). Posting — test_post."""

from __future__ import annotations

import json
import unittest

from helpers import IsolatedTest

from aimost import questions as q

AUTH = {"secret": "s3", "kid": "k1"}
URL = "http://edge.test:8767/action"


class Spec(IsolatedTest):
    def test_from_args(self):
        s = q.from_args(["Deploy now?", "yes", "no", "--rec", "2"])
        self.assertEqual(s["parts"], [{"title": "Deploy now?", "text": "", "options": ["yes", "no"], "rec": 2}])

    def test_rec_by_text_and_string_number(self):
        s = q.norm_rec({"parts": [{"options": ["a", "b"], "rec": "b"}, {"options": ["a", "b"], "rec": "1"}]})
        self.assertEqual([p["rec"] for p in s["parts"]], [2, 1])
        with self.assertRaises(q.AskError):
            q.norm_rec({"parts": [{"options": ["a", "b"], "rec": 3}]})
        with self.assertRaises(q.AskError):
            q.norm_rec({"parts": [{"options": ["a", "b"], "rec": True}]})

    def test_json_spec(self):
        f = self.tmp / "q.json"
        f.write_text(json.dumps({"context": "c", "parts": [{"title": "t", "options": ["x", "y"], "rec": "y"}]}))
        s = q.from_args([str(f)])
        self.assertEqual((s["context"], s["parts"][0]["rec"], s["parts"][0]["text"]), ("c", 2, ""))

    def test_refusals(self):
        for args in (["Q?", "only"], ["Q?"], []):
            with self.assertRaises(q.AskError) as e:
                q.from_args(args)
            self.assertEqual(e.exception.code, 2)
        for t in ("x" * 301, "line one\nline two"):
            with self.assertRaises(q.AskError) as e:
                q.from_args([t, "a", "b"])
            self.assertEqual(e.exception.code, 4)
        q.from_args(["x" * 300, "a", "b"])


class Render(unittest.TestCase):
    spec = {"context": "Release 2", "parts": [{"title": "Deploy?", "text": "why", "options": ["yes", "no"], "rec": 1},
                                              {"title": "Notify?", "text": "", "options": ["a", "b"]}]}

    def test_buttons_signed_and_slim(self):
        msg, atts = q.render("ask-1", self.spec, {}, "deployer", AUTH, URL)
        self.assertEqual(msg, "❓ Release 2\n_Answered 0 of 2._")
        acts = atts[0]["actions"]
        self.assertEqual([a["name"] for a in acts], ["⭐ yes", "no", q.CUSTOM])
        ctx = acts[1]["integration"]["context"]
        self.assertEqual(ctx, {"q": "ask-1", "part": 1, "secret": "s3", "kid": "k1", "agent": "deployer", "option": 2})
        self.assertEqual(acts[1]["integration"]["url"], URL)
        self.assertTrue(acts[2]["integration"]["context"]["custom"])
        self.assertNotIn("spec", json.dumps(acts))

    def test_size_is_linear(self):
        big = {"context": "", "parts": [{"title": f"q{n}", "text": "x" * 600, "options": ["a", "b", "c"]}
                                        for n in range(10)]}
        msg, atts = q.render("ask-1", big, {}, "r", AUTH, URL)
        self.assertLess(len(json.dumps(atts)), 100_000)

    def test_mentions_first_line_and_titles_in_body(self):
        s = dict(self.spec, mention=["ann", "bob"], context="")
        msg, _ = q.render("ask-1", s, {}, "r", AUTH, URL)
        self.assertEqual(msg.split("\n")[:3], ["@ann @bob", "❓ 1. Deploy?", "2. Notify?"])

    def test_already_mentioned_not_repeated(self):
        s = dict(self.spec, mention=["ann"], context="@ann-bot writes; @ann please look")
        self.assertTrue(q.render("ask-1", s, {}, "r", AUTH, URL)[0].startswith("❓ @ann-bot"))
        s = dict(self.spec, mention=["ann"], context="@ann-bot writes")
        self.assertTrue(q.render("ask-1", s, {}, "r", AUTH, URL)[0].startswith("@ann\n"))

    def test_partial_and_done(self):
        msg, atts = q.render("ask-1", self.spec, {"1": {"option": 1, "by": "ann"}}, "r", AUTH, URL)
        self.assertIn("_Answered 1 of 2._", msg)
        self.assertNotIn("actions", atts[0])
        self.assertIn("✅ **yes** — @ann", atts[0]["text"])
        msg, _ = q.render("ask-1", self.spec, {"1": {"option": 1, "by": "ann"}, "2": {"text": "later", "by": "bob"}},
                          "r", AUTH, URL)
        self.assertEqual(msg, "✅ Release 2\nDeploy? → **yes** — @ann\nNotify? → **«later»** — @bob")

    def test_discuss_button_until_done(self):
        s = dict(self.spec, discuss=True, to=["ann"])
        _, atts = q.render("ask-1", s, {}, "r", AUTH, URL)
        d = atts[-1]["actions"][0]
        self.assertEqual((d["name"], d["integration"]["context"]["kind"], d["integration"]["context"]["to"]),
                         (q.DISCUSS, "discuss", ["ann"]))
        _, atts = q.render("ask-1", s, {"1": {"option": 1, "by": "a"}, "2": {"option": 1, "by": "a"}}, "r", AUTH, URL)
        self.assertNotIn(q.DISCUSS, json.dumps(atts, ensure_ascii=False))


class Misc(unittest.TestCase):
    def test_action_url(self):
        self.assertEqual(q.action_url({"EDGE_PUBLIC": "edge:8767"}), "http://edge:8767/action")
        self.assertEqual(q.action_url({"EDGE_PUBLIC": "https://edge.test/"}), "https://edge.test/action")
        self.assertEqual(q.client_auth({"EDGE_SECRET": "s"}), {"secret": "s"})

    def test_title(self):
        self.assertEqual(q.title(Render.spec), "Deploy? (+1)")

    def test_cancelled_patch_keeps_props(self):
        post = {"message": "❓ Q", "props": {"agent": "r", "aimost_question": "ask-1", "attachments": [
            {"title": "1. Q", "text": "t", "actions": [{"id": "x"}]}]}}
        p = q.cancelled_patch(post, "moot", by="r", at=5)
        self.assertEqual(p["message"], "⏹ Question cancelled: moot\n❓ Q")
        self.assertEqual(p["props"]["attachments"], [{"title": "1. Q", "text": "t"}])
        self.assertEqual(p["props"]["aimost_cancelled"], {"why": "moot", "at": 5, "by": "r"})
        self.assertEqual((p["props"]["agent"], p["props"]["aimost_question"]), ("r", "ask-1"))
        self.assertIn("actions", post["props"]["attachments"][0])


if __name__ == "__main__":
    unittest.main()
