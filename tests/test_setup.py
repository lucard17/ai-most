"""`lmost setup` — directories and the connection check."""

from __future__ import annotations

import stat

from helpers import IsolatedTest
from itenv import IT, requires_mattermost


class Offline(IsolatedTest):
    def test_install_makes_private_dirs_and_reports_no_connection(self):
        r = self.lmost("setup")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no connections", r.stdout)
        for d in ("config", "config/connections", "config/bots", "state"):
            self.assertEqual(stat.S_IMODE((self.tmp / d).stat().st_mode), 0o700, d)

    def test_usage(self):
        self.assertEqual(self.lmost("setup", "--nope").returncode, 2)

    def test_missing_owner_and_bots(self):
        (self.tmp / "config" / "connections").mkdir(parents=True)
        (self.tmp / "config" / "connections" / "a.env").write_text("URL=http://127.0.0.1:9\n")
        (self.tmp / "config" / "connections" / "b.env").write_text("URL=http://127.0.0.1:9\nOWNER=x\n")
        r = self.lmost("setup", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("a: URL or OWNER missing", r.stdout)
        self.assertIn("b: no bots", r.stdout)


@requires_mattermost
class Real(IsolatedTest):
    def write(self, owner):
        cfg = self.tmp / "config"
        (cfg / "connections").mkdir(parents=True, exist_ok=True)
        (cfg / "bots" / "it").mkdir(parents=True, exist_ok=True)
        (cfg / "connections" / "it.env").write_text(f"URL={IT['LMOST_IT_URL']}\nOWNER={owner}\n")
        (cfg / "bots" / "it" / "deployer").write_text(IT["LMOST_IT_BOT_DEPLOYER"])

    def test_all_fine(self):
        self.write("owner")
        r = self.lmost("setup", "--check")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("✓ it: deployer → @deployer", r.stdout)
        self.assertIn("✓ it: OWNER @owner exists", r.stdout)

    def test_bad_token(self):
        self.write("owner")
        (self.tmp / "config" / "bots" / "it" / "broken").write_text("nope")
        r = self.lmost("setup", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("✗ it: broken — token does not answer", r.stdout)
        self.assertIn("✓ it: OWNER @owner exists", r.stdout, "checked with a token that works")

    def test_unknown_owner(self):
        self.write("nobody-here")
        r = self.lmost("setup", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("✗ it: OWNER @nobody-here not found", r.stdout)
