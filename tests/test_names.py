"""The props prefix (``names``): another prefix changes every name ai-most leaves in the chat and nothing else — the
behaviour suites run again under ``AIMOST_PROPS_PREFIX=chat`` with their expected names rewritten."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers

from aimost import names

TESTS = Path(__file__).resolve().parent
SUITES = ["test_edge", "test_questions", "test_waiting", "test_confirm", "test_post", "test_commands", "test_link",
          "test_agents", "test_askstore", "test_activity", "test_hooks"]
NAMES = sorted(set(re.findall(r'P\("([a-z_]+)"\)', "".join(p.read_text() for p in (Path(helpers.SRC) / "aimost").glob("*.py")))))


def under(prefix: str, name: str) -> str:
    out = subprocess.run([sys.executable, "-c", f"from aimost.names import P; print(P({name!r}))"], capture_output=True,
                         text=True, env=dict(os.environ, PYTHONPATH=helpers.SRC, AIMOST_PROPS_PREFIX=prefix))
    return out.stdout.strip() or out.stderr.strip()


class Names(unittest.TestCase):
    def test_default_and_other_prefix(self):
        self.assertEqual(names.P("answers"), "aimost_answers")
        self.assertEqual(under("chat", "answers"), "chat_answers")
        self.assertEqual(under("chat", "agents"), "chat_agents_catalog", "the earlier version's own spelling")
        self.assertEqual(under("team", "agents"), "team_agents")
        self.assertIn("AIMOST_PROPS_PREFIX", under("Bad-1", "answers"), "a bad prefix stops the process")

    def test_prefix_from_the_config_file(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "props-prefix").write_text("chat\n")
            out = subprocess.run([sys.executable, "-c", "from aimost.names import P; print(P('answers'))"], text=True,
                                 capture_output=True, env={**{k: v for k, v in os.environ.items() if not k.startswith("AIMOST_")},
                                                           "PYTHONPATH": helpers.SRC, "AIMOST_CONFIG_DIR": t})
            self.assertEqual(out.stdout.strip(), "chat_answers", out.stderr)

    def test_confirm_prefix_of_the_earlier_version(self):
        code = ("from aimost import confirm as c; print(bool(c.LEADING_RE.match('CHAT_CONFIRM=c0ffee00 x')),"
                " bool(c.LEADING_RE.match('AIMOST_CONFIRM=c0ffee00 x')))")
        run = lambda prefix: subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                            env=dict(os.environ, PYTHONPATH=helpers.SRC, AIMOST_PROPS_PREFIX=prefix)).stdout.split()
        self.assertEqual(run("chat"), ["True", "True"], "CHAT_CONFIRM=<id> spends a confirmation under prefix chat")
        self.assertEqual(run("aimost"), ["False", "True"], "only AIMOST_CONFIRM by default")

    def test_the_names_found(self):
        self.assertTrue({"answers", "ask", "question", "confirm", "pin", "unfollow", "heartbeat", "command"} <= set(NAMES))


class SameBehaviourUnderAnotherPrefix(unittest.TestCase):
    def test_suites_pass_with_prefix_chat(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            shutil.copytree(TESTS, t / "tests", ignore=shutil.ignore_patterns("__pycache__", "integration", "test_names.py"))
            for x in ("src", "bin", "hooks", "commands", ".claude-plugin", "pyproject.toml", "README.md", "LICENSE"):
                (t / x).symlink_to(TESTS.parent / x)
            pat = re.compile(r"aimost_(" + "|".join(sorted(NAMES, key=len, reverse=True)) + r")\b")
            for f in (t / "tests").glob("*.py"):
                s = f.read_text()
                f.write_text(pat.sub(lambda m: "chat_agents_catalog" if m.group(1) == "agents" else "chat_" + m.group(1), s))
            env = {k: v for k, v in os.environ.items() if not k.startswith("AIMOST_")}
            env.update(AIMOST_PROPS_PREFIX="chat", AIMOST_IT="0", PYTHONPATH=helpers.SRC)
            r = subprocess.run([sys.executable, "-m", "unittest", *SUITES], cwd=t / "tests", capture_output=True, text=True,
                               env=env, timeout=600)
            self.assertEqual(r.returncode, 0, r.stderr[-3000:])
            self.assertRegex(r.stderr, r"Ran \d+ tests")
