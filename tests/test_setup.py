"""`aimost setup` — directories, templates, services (fake systemctl, temp dirs) and the connection check."""

from __future__ import annotations

import os
import plistlib
import stat
import sys

from helpers import IsolatedTest
from itenv import IT, requires_mattermost

from aimost import install

SETUP_ENV = ("AIMOST_SERVICE_MANAGER", "AIMOST_SYSTEMCTL", "AIMOST_UNIT_DIR", "AIMOST_LAUNCHD_DIR")


class SetupTest(IsolatedTest):
    """Never the machine's own systemd: no service manager unless a test brings a fake one."""

    def setUp(self):
        super().setUp()
        self._setup_env = {k: os.environ.get(k) for k in SETUP_ENV}
        os.environ["AIMOST_SERVICE_MANAGER"] = "none"

    def tearDown(self):
        for k, v in self._setup_env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        super().tearDown()


class Offline(SetupTest):
    def test_install_makes_private_dirs_and_reports_no_connection(self):
        r = self.aimost("setup")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no connections", r.stdout)
        for d in ("config", "config/connections", "config/bots", "state"):
            self.assertEqual(stat.S_IMODE((self.tmp / d).stat().st_mode), 0o700, d)

    def test_usage(self):
        self.assertEqual(self.aimost("setup", "--nope").returncode, 2)

    def test_missing_owner_and_bots(self):
        (self.tmp / "config" / "connections").mkdir(parents=True)
        (self.tmp / "config" / "connections" / "a.env").write_text("URL=http://127.0.0.1:9\n")
        (self.tmp / "config" / "connections" / "b.env").write_text("URL=http://127.0.0.1:9\nOWNER=x\n")
        r = self.aimost("setup", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("a: URL or OWNER missing", r.stdout)
        self.assertIn("b: no bots", r.stdout)


@requires_mattermost
class Real(SetupTest):
    def write(self, owner):
        cfg = self.tmp / "config"
        (cfg / "connections").mkdir(parents=True, exist_ok=True)
        (cfg / "bots" / "it").mkdir(parents=True, exist_ok=True)
        (cfg / "connections" / "it.env").write_text(f"URL={IT['AIMOST_IT_URL']}\nOWNER={owner}\n")
        (cfg / "bots" / "it" / "deployer").write_text(IT["AIMOST_IT_BOT_DEPLOYER"])

    def test_all_fine(self):
        self.write("owner")
        r = self.aimost("setup", "--check")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("✓ it: deployer → @deployer", r.stdout)
        self.assertIn("✓ it: OWNER @owner exists", r.stdout)

    def test_bad_token(self):
        self.write("owner")
        (self.tmp / "config" / "bots" / "it" / "broken").write_text("nope")
        r = self.aimost("setup", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("✗ it: broken — token does not answer", r.stdout)
        self.assertIn("✓ it: OWNER @owner exists", r.stdout, "checked with a token that works")

    def test_unknown_owner(self):
        self.write("nobody-here")
        r = self.aimost("setup", "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("✗ it: OWNER @nobody-here not found", r.stdout)


FAKE_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$(dirname "$0")/calls"
case "$*" in *is-active*) grep -qx "${@: -1}" "$(dirname "$0")/active" 2>/dev/null || exit 3;; esac
exit 0
"""


class Services(SetupTest):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "fakebin"
        self.bin.mkdir()
        sc = self.bin / "systemctl"
        sc.write_text(FAKE_SYSTEMCTL.replace("#!/bin/sh", "#!/bin/bash"))
        sc.chmod(0o755)
        os.environ.update({"AIMOST_SERVICE_MANAGER": "systemd", "AIMOST_SYSTEMCTL": str(sc),
                           "AIMOST_UNIT_DIR": str(self.tmp / "units")})

    def calls(self):
        try:
            lines = (self.bin / "calls").read_text().splitlines()
        except FileNotFoundError:
            return []
        (self.bin / "calls").unlink()
        return lines

    def add_bot(self):
        d = self.tmp / "config" / "bots" / "it"
        d.mkdir(parents=True, exist_ok=True)
        (d / "deployer").write_text("tok")

    def test_units_rendered_not_started_without_bots(self):
        r = self.aimost("setup")
        self.assertIn("units installed but not started", r.stdout)
        for u in install.CORE_UNITS:
            text = (self.tmp / "units" / u).read_text()
            self.assertNotIn("@", text)
            self.assertIn(f"ExecStart={sys.executable} -m aimost ", text)
            pp = text.split("PYTHONPATH=", 1)[1].splitlines()[0]
            self.assertTrue(os.path.isfile(os.path.join(pp, "aimost", "__init__.py")), pp)
        self.assertEqual(self.calls()[:1], ["--user daemon-reload"])
        self.assertIn("✗ aimost-link.service not active", r.stdout)

    def test_started_with_a_bot_and_quiet_on_repeat(self):
        self.add_bot()
        self.aimost("setup")
        c = self.calls()
        self.assertIn("--user enable -q aimost-link.service aimost-dispatch.service", c)
        self.assertIn("--user restart aimost-link.service", c)
        self.assertIn("--user restart aimost-dispatch.service", c)
        (self.bin / "active").write_text("aimost-link.service\naimost-dispatch.service\n")
        before = (self.tmp / "units" / "aimost-link.service").stat().st_mtime_ns
        r = self.aimost("setup")
        self.assertNotIn("+ ", "".join(x for x in r.stdout.splitlines() if "units" in x))
        self.assertEqual((self.tmp / "units" / "aimost-link.service").stat().st_mtime_ns, before)
        self.assertFalse([x for x in self.calls() if "restart" in x], "a working bridge is not cut")
        self.assertIn("✓ aimost-link.service active", r.stdout)
        # a changed unit is rewritten and only that one restarted
        (self.tmp / "units" / "aimost-dispatch.service").write_text("old")
        self.aimost("setup")
        self.assertEqual([x for x in self.calls() if "restart" in x], ["--user restart aimost-dispatch.service"])

    def test_plugin_service_and_timer(self):
        self.add_bot()
        install.SERVICES.append(("aimost-extra.timer", "[Timer]\nOnUnitActiveSec=2min\n# @PYTHON@\n"))
        try:
            changed = install.install_systemd(start=True, out=lambda m: None)
        finally:
            install.SERVICES.clear()
        self.assertIn("aimost-extra.timer", changed)
        self.assertIn(sys.executable, (self.tmp / "units" / "aimost-extra.timer").read_text())
        self.assertIn("--user enable --now -q aimost-extra.timer", self.calls())

    def test_templates_once_never_over_yours(self):
        self.aimost("setup")
        cfg = self.tmp / "config"
        for name in ("confirm-patterns", "confirm-deny", "private-patterns"):
            self.assertEqual(stat.S_IMODE((cfg / name).stat().st_mode), 0o600, name)
        self.assertIn("terraform", (cfg / "confirm-patterns").read_text())
        (cfg / "private-patterns").write_text("mine\n")
        r = self.aimost("setup")
        self.assertEqual((cfg / "private-patterns").read_text(), "mine\n")
        self.assertIn("! no connection: cp ", r.stdout)

    def test_launchd(self):
        os.environ.update({"AIMOST_SERVICE_MANAGER": "launchd", "AIMOST_LAUNCHD_DIR": str(self.tmp / "agents")})
        r = self.aimost("setup")
        self.assertIn("launchctl bootstrap gui/$(id -u)", r.stdout)
        for n in ("link", "dispatch"):
            d = plistlib.loads((self.tmp / "agents" / f"io.github.lucard17.aimost-{n}.plist").read_bytes())
            self.assertEqual(d["ProgramArguments"], [sys.executable, "-m", "aimost", n, "serve"])
            self.assertTrue(os.path.isdir(os.path.dirname(d["StandardErrorPath"])))
        self.assertEqual(self.calls(), [], "launchd mode does not call systemctl")

    def test_no_services_flag(self):
        r = self.aimost("setup", "--no-services")
        self.assertIn("no services installed", r.stdout)
        self.assertFalse((self.tmp / "units").exists())
        self.assertEqual(self.calls(), [])


class FollowsPluginUpdate(Services):
    """After a plugin update the services move to the new version on the next SessionStart (hook `units`)."""

    def pin_units(self, version):
        """Point the core units at a fake older package of `version` ("" — a removed directory)."""
        pkg = self.tmp / f"old-{version or 'gone'}"
        if version:
            (pkg / "aimost").mkdir(parents=True)
            (pkg / "aimost" / "__init__.py").write_text(f'__version__ = "{version}"\n')
        for u in install.CORE_UNITS:
            p = self.tmp / "units" / u
            p.write_text(p.read_text().replace(p.read_text().split("PYTHONPATH=", 1)[1].splitlines()[0], str(pkg)))
        (self.bin / "active").write_text("aimost-link.service\naimost-dispatch.service\n")
        self.calls()

    def restarts(self):
        return sorted(x for x in self.calls() if "restart" in x)

    def test_older_or_removed_version_moves_to_this_one(self):
        from aimost import hooks
        self.add_bot()
        self.aimost("setup")
        for old in ("0.0.1", ""):
            self.pin_units(old)
            r = hooks.run("units", "{}")
            self.assertIn("background services moved to", r.stdout)
            self.assertEqual(install.unit_version(self.tmp / "units" / "aimost-link.service"), aimost_version())
            self.assertEqual(self.restarts(), ["--user restart aimost-dispatch.service", "--user restart aimost-link.service"])

    def test_same_or_newer_untouched(self):
        from aimost import hooks
        self.add_bot()
        self.aimost("setup")
        (self.bin / "active").write_text("aimost-link.service\naimost-dispatch.service\n")
        self.calls()
        self.assertEqual(hooks.run("units", "{}").stdout, "")
        self.pin_units("99.0.0")   # a session still on the old plugin never downgrades the services
        self.assertEqual(hooks.run("units", "{}").stdout, "")
        self.assertEqual(self.restarts(), [])
        self.assertEqual(install.unit_version(self.tmp / "units" / "aimost-link.service"), "99.0.0")

    def test_nothing_without_installed_units(self):
        from aimost import hooks
        self.assertEqual(hooks.run("units", "{}").stdout, "")
        self.assertFalse((self.tmp / "units").exists())


def aimost_version():
    from aimost import __version__
    return __version__
