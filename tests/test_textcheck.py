"""Outgoing text checks — offline (spec: text, extensions → text checks). Server checks run in test_post."""

from __future__ import annotations

import unittest

from helpers import IsolatedTest

from aimost import paths, textcheck as tc


class QuestionsAsText(unittest.TestCase):
    def test_count(self):
        self.assertEqual(tc.count_questions("Deploy? Or wait?"), 2)
        self.assertEqual(tc.count_questions("One question?"), 1)
        self.assertEqual(tc.count_questions("`a?` and ```\nb? c?\n``` and https://x.test/?q=1 ok"), 0)
        self.assertEqual(tc.count_questions("(really?) «so?»"), 2)

    def test_refuse_two_unless_plain(self):
        with self.assertRaises(tc.Refused) as r:
            tc.run(tc.Draft("Deploy? Or wait?"))
        self.assertEqual(r.exception.code, 4)
        self.assertIn("--ask", r.exception.hint)
        tc.run(tc.Draft("Deploy? Or wait?", plain=True))
        tc.run(tc.Draft("Deploy?"))


class Layout(unittest.TestCase):
    """A wall of text is refused, code 11 (Oracle: upstream chat-post-markup.test.sh)."""
    WALL = "We looked at the failing upload and " + "found that the retry loop gives up too early, " * 12

    def test_problem(self):
        from aimost import markup
        self.assertIsNone(markup.problem(""))
        self.assertIsNone(markup.problem("Short and to the point."))
        self.assertIn("the first line is", markup.problem("x" * 201))
        self.assertIsNone(markup.problem("@ann @bob\n" + "x" * 150), "a line of mentions is not the first line")
        para = "\n".join(["short point"] + ["word " * 15] * 5)
        self.assertIn("a paragraph of", markup.problem(para))
        self.assertIn("a line of", markup.problem("Point.\n**What:** " + "a; " * 130))
        table = "Status.\n| part | MR |\n| --- | --- |\n| core | " + "repo!1 <br>" * 90 + " |"
        self.assertIsNone(markup.problem(table), "a table row is not limited: a cell does not break into lines")
        flat = "\n\n".join(["Point."] + ["a short paragraph " * 5] * 6)
        self.assertIn("without a single field, list or heading", markup.problem(flat))
        good = "Point.\n\n**What:** a change\n**Why:** a reason\n\n" + "\n".join("- item " * 6 for _ in range(10))
        self.assertIsNone(markup.problem(good))
        code = "Point.\n```\n" + ("x " * 400) + "\n```\n> " + ("quoted " * 80)
        self.assertIsNone(markup.problem(code), "code blocks and quotes do not count")
        link = "Point: [the report](https://x.test/" + "a" * 400 + ")"
        self.assertIsNone(markup.problem(link), "link addresses do not count")

    def test_in_the_pipeline_only_where_asked(self):
        with self.assertRaises(tc.Refused) as r:
            tc.run(tc.Draft(self.WALL, layout=True))
        self.assertEqual(r.exception.code, 11)
        self.assertIn("--plain", r.exception.hint)
        tc.run(tc.Draft(self.WALL, layout=True, plain=True))
        tc.run(tc.Draft(self.WALL))                      # a document, a pin, a tool's text — not asked
        with self.assertRaises(tc.Refused):
            tc.run(tc.Draft("", ask={"context": self.WALL, "parts": []}, layout=True))
        tc.run(tc.Draft("", ask={"context": "Which window?", "parts": []}, layout=True))


class PrivateGuard(IsolatedTest):
    def write_patterns(self, *lines):
        paths.config_dir().mkdir(parents=True, exist_ok=True)
        (paths.config_dir() / "private-patterns").write_text("\n".join(("# comment",) + lines) + "\n")

    def test_off_by_default(self):
        tc.run(tc.Draft("my-hobby project news"))

    def test_missing_patterns_refuse(self):
        with self.assertRaises(tc.Refused) as r:
            tc.run(tc.Draft("x", conn={"PRIVATE_GUARD": "on"}))
        self.assertEqual(r.exception.code, 5)

    def test_text_ask_and_file_are_checked(self):
        self.write_patterns(r"my-hobby")
        on = {"PRIVATE_GUARD": "on"}
        tc.run(tc.Draft("work only", conn=on))
        for d in (tc.Draft("news on MY-HOBBY", conn=on),
                  tc.Draft("", ask={"context": "", "parts": [{"title": "t", "text": "about my-hobby"}]}, conn=on),
                  tc.Draft("", extra=["header my-hobby"], conn=on)):
            with self.assertRaises(tc.Refused) as r:
                tc.run(d)
            self.assertEqual(r.exception.code, 5)
        note = self.tmp / "n.md"
        note.write_text("lies in my-hobby\n")
        with self.assertRaises(tc.Refused):
            tc.run(tc.Draft("", files=[str(note)], conn=on))
        named = self.tmp / "my-hobby.png"
        named.write_bytes(b"\x89PNG\0\0")
        with self.assertRaises(tc.Refused):
            tc.run(tc.Draft("", files=[str(named)], conn=on))


class Mentions(unittest.TestCase):
    def test_mentions_outside_code(self):
        self.assertEqual(tc.mentions("hi @Ann and @bob.k, `@types/node`, a@b.c, @here"), {"ann", "bob.k"})

    def test_boundary(self):
        self.assertTrue(tc.mentioned("ask @ann.", "ann"))
        self.assertFalse(tc.mentioned("ask @ann-bot", "ann"))
        self.assertFalse(tc.mentioned("ask @annie", "ann"))


class Pipeline(unittest.TestCase):
    def test_plugin_refusal_code(self):
        def nope(d):
            raise tc.Refused(21, "tracker key without title")
        with self.assertRaises(tc.Refused) as r:
            tc.run(tc.Draft("x"), plugins=[nope])
        self.assertEqual((r.exception.code, r.exception.hint), (21, "tracker key without title"))

    def test_plugin_code_outside_range_becomes_20(self):
        def bad(d):
            raise tc.Refused(5, "pretends to be the private guard")
        with self.assertRaises(tc.Refused) as r:
            tc.run(tc.Draft("x"), plugins=[bad])
        self.assertEqual(r.exception.code, 20)

    def test_rewrite_and_backend_down(self):
        def link(d):
            d.map_text(lambda t: t.replace("KEY-1", "[KEY-1](https://tracker.test/KEY-1)"))

        def down(d):
            raise ConnectionError("tracker unreachable")
        d = tc.run(tc.Draft("see KEY-1", chunks=["part KEY-1"],
                            ask={"context": "KEY-1", "parts": [{"title": "KEY-1?", "text": ""}]}), plugins=[down, link])
        self.assertEqual(d.message, "see [KEY-1](https://tracker.test/KEY-1)")
        self.assertEqual(d.chunks, ["part [KEY-1](https://tracker.test/KEY-1)"])
        self.assertEqual(d.ask["parts"][0]["title"], "[KEY-1](https://tracker.test/KEY-1)?")
        self.assertTrue(any("down skipped" in n for n in d.notes))

    def test_builtins_before_plugins(self):
        seen = []
        with self.assertRaises(tc.Refused) as r:
            tc.run(tc.Draft("A? B?"), plugins=[lambda d: seen.append(1)])
        self.assertEqual((r.exception.code, seen), (4, []))


class MdChunks(unittest.TestCase):
    def test_short(self):
        self.assertEqual(tc.md_chunks("# T\n\nbody"), ["# T\n\nbody"])

    def test_split_keeps_code_fences(self):
        text = "# A\n" + "x\n" * 30 + "```\n" + "code\n" * 30 + "```\n" + "# B\nend"
        parts = tc.md_chunks(text, limit=100)
        self.assertGreater(len(parts), 2)
        for p in parts:
            self.assertEqual(p.count("```") % 2, 0, p)
            self.assertLessEqual(len(p), 120)
        self.assertEqual("".join(parts).count("code"), 30)

    def test_prefers_heading(self):
        parts = tc.md_chunks("# A\n" + "a" * 60 + "\n# B\nb", limit=100)
        self.assertEqual(parts, ["# A\n" + "a" * 60, "# B\nb"])


if __name__ == "__main__":
    unittest.main()
