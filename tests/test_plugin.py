"""Claude Code plugin (repo root is the plugin): manifest, hook registration, the hook wrapper stays silent without the
bridge (delivery spec: hooks stay silent where there is no chat)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from helpers import IsolatedTest

from aimost import __version__, hooks

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "bin" / "aimost-hook"


class Manifest(IsolatedTest):
    def test_plugin_and_marketplace(self):
        p = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual((p["name"], p["version"], p["license"]), ("ai-most", __version__, "MIT"))
        m = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual([(x["name"], x["source"]) for x in m["plugins"]], [("ai-most", "./")])

    def test_hooks_registered_as_the_hook_bodies_expect(self):
        reg = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        got = set()
        for event, groups in reg.items():
            for g in groups:
                for h in g["hooks"]:
                    m = re.fullmatch(r"\$\{CLAUDE_PLUGIN_ROOT\}/bin/aimost-hook ([a-z-]+)", h["command"])
                    self.assertTrue(m, h["command"])
                    self.assertIn(m.group(1), set(hooks.HOOKS) | {"gitlab"})
                    got.add((event, g.get("matcher", ""), m.group(1)))
        self.assertEqual(got, {("PostToolUse", "*", "inbox"), ("PostToolUse", "Bash", "gitlab"), ("Stop", "", "inbox"),
                               ("PreToolUse", "AskUserQuestion", "ask-guard"),
                               ("PreToolUse", "Bash", "confirm-guard"),
                               ("PreToolUse", "Bash|Edit|Write|NotebookEdit", "peer-guard")})

    def test_setup_command(self):
        text = (ROOT / "commands" / "setup.md").read_text()
        self.assertTrue(text.startswith("---\ndescription: "))
        self.assertIn("aimost setup $ARGUMENTS", text)


class Wrapper(IsolatedTest):
    """The wrapper with a recording fake python: proves when Python is (not) started at all."""

    def setUp(self):
        super().setUp()
        self.rec = self.tmp / "py-called"
        fake = self.tmp / "fakepy"
        fake.write_text(f'#!/bin/sh\necho "$PYTHONPATH|$*" > {self.rec}\n')
        fake.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("AIMOST_", "XDG_"))}
        self.env.update(HOME=str(self.tmp / "home"), AIMOST_PYTHON=str(fake))

    def run_hook(self, name, **env):
        return subprocess.run([str(HOOK), name], input="{}", capture_output=True, text=True, env=dict(self.env, **env))

    def home_files(self):
        return sorted(str(p.relative_to(self.tmp / "home")) for p in (self.tmp / "home").rglob("*"))

    def test_silent_without_the_bridge(self):
        for name in list(hooks.HOOKS) + ["gitlab"]:
            r = self.run_hook(name)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), name)
        self.assertFalse(self.rec.exists(), "no Python start without the bridge")
        self.assertEqual(self.home_files(), [])

    def test_runs_where_the_bridge_is(self):
        state = self.tmp / "home" / ".local" / "state" / "aimost"
        (state / "inbox").mkdir(parents=True)
        self.run_hook("inbox")
        pp, args = self.rec.read_text().strip().split("|")
        self.assertEqual(args, "-m aimost hook inbox")
        self.assertEqual(pp.split(":")[0], str(ROOT / "src"))
        self.rec.unlink()
        self.run_hook("gitlab")
        self.assertEqual(self.rec.read_text().strip().split("|")[1], "-m aimost gitlab hook")
        self.rec.unlink()
        self.run_hook("confirm-guard")
        self.assertFalse(self.rec.exists(), "guards look at the marks directory")
        (state / "marks").mkdir()
        self.run_hook("peer-guard")
        self.assertTrue(self.rec.exists())

    def test_overrides_respected(self):
        (self.tmp / "inb").mkdir()
        self.run_hook("ask-guard", AIMOST_INBOX_DIR=str(self.tmp / "inb"))
        self.assertTrue(self.rec.exists())

    def test_aimost_wrapper_runs_the_checkout(self):
        env = dict(self.env)
        env.pop("AIMOST_PYTHON")
        r = subprocess.run([str(ROOT / "bin" / "aimost"), "--version"], capture_output=True, text=True, env=env)
        self.assertEqual(r.stdout.strip(), __version__)
