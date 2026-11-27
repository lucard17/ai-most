"""Plugins (extensions spec): discovery, per-connection switching, transactional registration, source authority."""

from __future__ import annotations

import json
import textwrap

from helpers import IsolatedTest
from itenv import IT, requires_mattermost

from aimost import cli_who, commands, confirm, connections, hooks, inbox, plugins, textcheck


class Fresh(IsolatedTest):
    """Each test starts with empty registries and no loaded plugins; all are restored afterwards."""

    def setUp(self):
        super().setUp()
        self._reg = (list(textcheck.TEXT_CHECKS), list(connections.ADMISSION), list(connections.BOT_RESOLVERS),
                       list(confirm.GRANTS), dict(hooks.READ_RULES), list(cli_who.WHO_SOURCES),
                       dict(plugins.ANSWER_SINKS), dict(plugins._plugins), plugins._loaded)
        plugins._plugins.clear()
        plugins._loaded = False
        (self.tmp / "config" / "plugins").mkdir(parents=True)
        (self.tmp / "config" / "connections").mkdir(parents=True)

    def tearDown(self):
        (tc, ad, br, gr, rr, ws, sk, pl, ld) = self._reg
        textcheck.TEXT_CHECKS[:] = tc
        connections.ADMISSION[:] = ad
        connections.BOT_RESOLVERS[:] = br
        confirm.GRANTS[:] = gr
        hooks.READ_RULES.clear()
        hooks.READ_RULES.update(rr)
        cli_who.WHO_SOURCES[:] = ws
        plugins.ANSWER_SINKS.clear()
        plugins.ANSWER_SINKS.update(sk)
        plugins._plugins.clear()
        plugins._plugins.update(pl)
        plugins._loaded = ld
        super().tearDown()

    def plugin(self, name, body):
        (self.tmp / "config" / "plugins" / f"{name}.py").write_text(textwrap.dedent(body))

    def conn(self, name, extra=""):
        (self.tmp / "config" / "connections" / f"{name}.env").write_text(
            f"URL=http://127.0.0.1:9\nOWNER=o\nSESSIONS=r\n{extra}")
        return connections.connections()[name]


REFUSER = """
    NAME = "strict"
    VERSION = "1.2"
    KINDS = ("text_check",)
    from aimost import textcheck
    def no_tbd(d):
        if "TBD" in d.message:
            raise textcheck.Refused(21, "no TBD in posts")
    def register(reg):
        reg.text_check(no_tbd)
"""


class Discovery(Fresh):
    def test_builtin_listed_and_broken_plugin_reported(self):
        self.plugin("broken", "raise RuntimeError('boom')\n")
        self.plugin("strict", REFUSER)
        got = {p.name: p.row() for p in plugins.load()}
        self.assertEqual(got["gitlab"]["source"], "built-in")
        self.assertEqual(got["gitlab"]["kinds"], "source,text_check")
        self.assertEqual(got["broken"]["state"], "failed (RuntimeError)")
        self.assertEqual((got["strict"]["state"], got["strict"]["version"]), ("loaded", "1.2"))

    def test_cli_lists_and_survives_a_broken_plugin(self):
        self.plugin("broken", "import no_such_module_here\n")
        self.conn("a", "PLUGINS=strict\n")
        self.conn("b", "PLUGINS_OFF=gitlab\n")
        self.plugin("strict", REFUSER)
        r = self.aimost("plugins", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = {x["name"]: x for x in json.loads(r.stdout)}
        self.assertEqual(rows["broken"]["state"], "failed (ModuleNotFoundError)")
        self.assertEqual(rows["strict"]["enabled"], "a")
        self.assertEqual(rows["gitlab"]["enabled"], "a")
        self.assertIn("not loaded", r.stderr)
        r = self.aimost("plugins")
        self.assertIn("strict", r.stdout)
        self.assertEqual(self.aimost("agents", "--help").returncode, 2, "other commands still run")

    def test_register_failing_midway_leaves_nothing(self):
        self.plugin("half", """
            NAME = "half"
            KINDS = ("text_check", "admission")
            def register(reg):
                reg.text_check(lambda d: None)
                raise ValueError("second half broken")
        """)
        plugins.load()
        self.assertEqual([f for f in textcheck.TEXT_CHECKS if f.__name__.startswith("half.")], [])
        self.assertEqual(plugins._plugins["half"].error, "ValueError")

    def test_unknown_kind_refused(self):
        self.plugin("odd", "NAME='odd'\nKINDS=('teleport',)\ndef register(reg): pass\n")
        plugins.load()
        self.assertEqual(plugins._plugins["odd"].error, "ValueError")

    def test_load_once(self):
        self.plugin("strict", REFUSER)
        plugins.load()
        plugins.load()
        self.assertEqual(len([f for f in textcheck.TEXT_CHECKS if f.__name__ == "strict.no_tbd"]), 1)


class Switching(Fresh):
    def test_text_check_only_where_enabled(self):
        self.plugin("strict", REFUSER)
        plugins.load()
        on, off = self.conn("on", "PLUGINS=strict\n"), self.conn("off")
        with self.assertRaises(textcheck.Refused) as e:
            textcheck.run(textcheck.Draft("TBD later", conn=on, plain=True))
        self.assertEqual((e.exception.code, e.exception.hint), (21, "no TBD in posts"))
        textcheck.run(textcheck.Draft("TBD later", conn=off, plain=True))
        textcheck.run(textcheck.Draft("TBD later", conn=self.conn("x", "PLUGINS=strict\nPLUGINS_OFF=strict\n"),
                                      plain=True))

    def test_builtin_off_per_connection(self):
        plugins.load()
        self.assertTrue(plugins.enabled("gitlab", self.conn("a")))
        self.assertFalse(plugins.enabled("gitlab", self.conn("b", "PLUGINS_OFF=gitlab\n")))
        self.assertTrue(plugins.enabled("gitlab", None), "no connection — built-ins only")
        self.assertFalse(plugins.enabled("strict", None))

    def test_admission_and_sink_gated(self):
        self.plugin("door", """
            NAME = "door"
            KINDS = ("admission", "answer_sink")
            def register(reg):
                reg.admission(lambda conn, role: True if role.startswith("review-") else None)
                class Sink:
                    def answer(self, conn, q, part, answer, title):
                        return "noted"
                reg.answer_sink("tracker", Sink())
        """)
        plugins.load()
        on, off = self.conn("on", "PLUGINS=door\n"), self.conn("off")
        self.assertTrue(connections.admitted(on, "review-12"))
        self.assertFalse(connections.admitted(off, "review-12"))
        self.assertEqual(plugins.sink(on, "tracker").answer(on, "q1", 1, {}, "t"), "noted")
        self.assertEqual(list(plugins.sinks(on)), ["tracker"])
        self.assertEqual(plugins.sinks(off), {})
        self.assertIsNone(plugins.sink(off, "tracker"))
        self.assertIsNone(plugins.sink(on, "nope"))

    def test_global_points_need_one_enabled_connection(self):
        body = """
            NAME = "tools"
            KINDS = ("command",)
            from aimost import commands
            def register(reg):
                reg.read_rule("mytool", lambda args, ctx: None)
                reg.who_source(lambda term: [])
        """
        self.plugin("tools", body)
        self.conn("a")
        plugins.load()
        self.assertNotIn("mytool", hooks.READ_RULES, "not enabled anywhere — not installed")
        plugins._plugins.clear()
        plugins._loaded = False
        cli_who.WHO_SOURCES[:] = self._reg[5]
        self.conn("a", "PLUGINS=tools\n")
        plugins.load()
        self.assertIn("mytool", hooks.READ_RULES)
        self.assertEqual(len(cli_who.WHO_SOURCES), len(self._reg[5]) + 2, "gitlab and tools")

    def test_grant_gated_by_the_roles_connection(self):
        self.plugin("ok", """
            NAME = "ok"
            KINDS = ()
            def register(reg):
                reg.grant(lambda role, cmd, inp: "merge question answered")
        """)
        plugins.load()
        self.conn("a")
        self.assertEqual([g("r", "x", {}) for g in confirm.GRANTS], [None])
        self.conn("a", "PLUGINS=ok\n")
        self.assertEqual([g("r", "x", {}) for g in confirm.GRANTS], ["merge question answered"])


class SourceAuthority(Fresh):
    def test_source_cannot_raise_the_role(self):
        self.plugin("feed", """
            NAME = "feed"
            KINDS = ("source",)
            REG = []
            def register(reg):
                REG.append(reg)
        """)
        plugins.load()
        import sys
        reg = sys.modules["aimost_local_plugin_feed"].REG[0]
        conn = self.conn("a", "PLUGINS=feed\n")
        e = reg.inbox("r", conn).put("deploy now", sender="o", role="owner", via="a")
        self.assertEqual((e["role"], e["kind"], e["from"]), ("peer", "feed", "o"))
        self.assertNotIn("via", e)
        self.assertFalse(inbox.is_peer(e), "no via — not a chat post either")
        conn = self.conn("a", "PLUGINS=feed\nSOURCES_OWNER=feed\n")
        self.assertEqual(reg.inbox("r", conn).put("x")["role"], "owner")


@requires_mattermost
class RefusalThroughPost(Fresh):
    """`aimost post` with a local plugin refusing: exit 21, the hint, nothing posted."""

    def test_refused_post(self):
        self.plugin("strict", REFUSER)
        (self.tmp / "config" / "connections" / "it.env").write_text(
            f"URL={IT['AIMOST_IT_URL']}\nOWNER=owner\nTEAM=it\nSESSIONS=deployer\nPLUGINS=strict\n")
        (self.tmp / "config" / "bots" / "it").mkdir(parents=True)
        (self.tmp / "config" / "bots" / "it" / "deployer").write_text(IT["AIMOST_IT_BOT_DEPLOYER"])
        r = self.aimost("post", "--via", "it", "plan: TBD", env={"AIMOST_ROLE": "deployer"})
        self.assertEqual(r.returncode, 21, r.stdout + r.stderr)
        self.assertIn("no TBD in posts", r.stderr)
        self.assertNotIn("posted", r.stdout)
        r = self.aimost("post", "--via", "it", "plan: ready", env={"AIMOST_ROLE": "deployer"})
        self.assertEqual(r.returncode, 0, r.stderr)
