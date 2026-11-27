"""`lmost migrate`: a fake chat-* setup is imported; the source is untouched; l-most reads the result."""

from __future__ import annotations

import json
import stat

from helpers import IsolatedTest

from lmost import connections, inbox


class Migrate(IsolatedTest):
    def setUp(self):
        super().setUp()
        h = self.tmp / "home"
        self.old_cfg = h / ".config" / "chat"
        self.old_state = h / ".local" / "state"
        (self.old_cfg / "connections").mkdir(parents=True)
        (self.old_cfg / "bots" / "work").mkdir(parents=True)
        (self.old_cfg / "connections" / "work.env").write_text("URL=https://chat.example\nOWNER=ann\nSESSIONS=deployer\n"
                                                               "PLAN=live\n")
        (self.old_cfg / "bots" / "work" / "deployer").write_text("tok-1")
        (self.old_cfg / "private-patterns").write_text("\\bhome-box\\b\n")
        (self.old_cfg / "default").write_text("work\n")
        (h / ".config" / "chat-inbox").mkdir()
        (h / ".config" / "chat-inbox" / "wake.list").write_text("deployer\n")
        ib = self.old_state / "chat-inbox"
        ib.mkdir(parents=True)
        (ib / "deployer.jsonl").write_text("".join(json.dumps({"ts": "t", "from": "ann", "text": f"m{i}"}) + "\n"
                                                   for i in range(3)))
        (ib / "deployer.cursor").write_text("1")
        (ib / "deployer.lock").write_text("")
        (self.old_state / "chat-activity" / "avatars").mkdir(parents=True)
        (self.old_state / "chat-activity" / "avatars" / "a.png").write_text("x")
        (self.old_state / "chat-activity" / "status.json").write_text("{}")
        (self.old_state / "chat-mr-watch").mkdir()
        for d, f in (("chat-confirm", "deployer.chat"), ("chat-link", "work.snooze.json"), ("chat-asks", "deployer.jsonl")):
            (self.old_state / d).mkdir()
            (self.old_state / d / f).write_text("1\n")

    def snapshot(self):
        return sorted((str(p), p.read_bytes()) for p in (self.tmp / "home").rglob("*") if p.is_file())

    def test_dry_run_changes_nothing(self):
        before = self.snapshot()
        r = self.lmost("migrate")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dry run: 11 to copy, 0 kept", r.stdout)
        self.assertIn("! work: PLAN is not read by l-most", r.stdout)
        self.assertIn("chat-mr-watch not imported", r.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.tmp / "config" / "connections").exists())

    def test_apply_and_lmost_reads_it(self):
        before = self.snapshot()
        r = self.lmost("migrate", "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.snapshot(), before, "the source is untouched")
        conn = connections.connections()["work"]
        self.assertEqual((conn["OWNER"], connections.bot(conn, "deployer")), ("ann", "tok-1"))
        self.assertEqual(connections.default_name(), "work")
        box = inbox.Inbox("deployer")
        self.assertEqual(box.count(), 2, "cursor kept: one of three already read")
        st = self.tmp / "state"
        self.assertFalse((st / "inbox" / "deployer.lock").exists())
        self.assertFalse((st / "activity" / "avatars").exists())
        self.assertTrue((st / "activity" / "status.json").exists())
        self.assertEqual((self.tmp / "config" / "wake.list").read_text(), "deployer\n")
        for rel in ("marks/deployer.chat", "link/work.snooze.json", "asks/deployer.jsonl"):
            self.assertTrue((st / rel).is_file(), rel)
        tok = self.tmp / "config" / "bots" / "work" / "deployer"
        self.assertEqual(stat.S_IMODE(tok.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(tok.parent.stat().st_mode), 0o700)

    def test_existing_files_kept(self):
        (self.tmp / "config").mkdir()
        (self.tmp / "config" / "private-patterns").write_text("mine\n")
        r = self.lmost("migrate", "--apply")
        self.assertIn("= kept", r.stdout)
        self.assertIn("copied 10", r.stdout)
        self.assertEqual((self.tmp / "config" / "private-patterns").read_text(), "mine\n")
        r = self.lmost("migrate", "--apply")
        self.assertIn("copied 0", r.stdout)

    def test_nothing_to_import(self):
        r = self.lmost("migrate", "--from-config", str(self.tmp / "none"), "--from-state", str(self.tmp / "none"))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.lmost("migrate", "--bogus").returncode, 2)
