"""Connections and admission — delivery spec ("Own connection and admission", "Shared bots and thread routing",
"Relay bot"), authority ("Weight of words", "Who confirms"). Oracle: chatlib*.test.sh, chat-session-cwd-mem.test.sh,
chat-relay.test.sh (chatlib part)."""

from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path

from helpers import IsolatedTest

from aimost import connections as cn
from aimost import paths


class Conf(IsolatedTest):
    def setUp(self):
        super().setUp()
        cn._cwd_mem.clear()
        os.environ["AIMOST_CWD_CACHE"] = str(self.tmp / "cwd-cache.json")
        self.addCleanup(os.environ.pop, "AIMOST_CWD_CACHE", None)
        self.claude = Path(os.environ["HOME"]) / ".claude"

    def conn(self, name, **kv):
        d = paths.config_dir() / "connections"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.env").write_text("# comment\n" + "".join(f"{k}={v}\n" for k, v in kv.items()))

    def botfile(self, conn, role, token="tok"):
        d = paths.config_dir() / "bots" / conn
        d.mkdir(parents=True, exist_ok=True)
        (d / role).write_text(token + "\n")

    def default(self, name):
        paths.config_dir().mkdir(parents=True, exist_ok=True)
        (paths.config_dir() / "default").write_text(name + "\n")

    def transcript(self, project, sid, title, cwd=None, size=0):
        p = self.claude / "projects" / project
        p.mkdir(parents=True, exist_ok=True)
        lines = [{"type": "user", "cwd": cwd, "sessionId": sid}] if cwd else []
        lines.append({"type": "custom-title", "customTitle": title, "sessionId": sid})
        body = "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines)
        if size:
            body += ('{"type":"assistant","text":"' + "x" * 1000 + '"}\n') * (size // 1000)
        (p / f"{sid}.jsonl").write_text(body)


class OwnConnection(Conf):
    def setUp(self):
        super().setUp()
        self.conn("home", SESSIONS="both,home-only")
        self.conn("work", SESSIONS="both,work-only")

    def test_default_first(self):
        self.default("work")
        self.assertEqual(cn.own_connection("both")["NAME"], "work")
        self.assertEqual(cn.own_connection("home-only")["NAME"], "home")
        self.assertEqual(cn.own_connection("work-only")["NAME"], "work")

    def test_without_default_first_alphabetically(self):
        self.assertEqual(cn.own_connection("both")["NAME"], "home")

    def test_not_admitted(self):
        self.assertIsNone(cn.own_connection("stranger"))


class Admission(Conf):
    def test_names_patterns_star_exclude(self):
        c = {"NAME": "w", "SESSIONS": "a, review-*", "EXCLUDE": "review-bad"}
        self.assertTrue(cn.admitted(c, "a"))
        self.assertTrue(cn.admitted(c, "review-x"))
        self.assertFalse(cn.admitted(c, "review-bad"))
        self.assertFalse(cn.admitted(c, "b", cwd="/nowhere"))
        self.assertTrue(cn.admitted({"SESSIONS": "*"}, "anything"))

    def test_by_directory(self):
        c = {"SESSIONS_CWD": "/home/u/proj"}
        self.assertTrue(cn.admitted(c, "x", cwd="/home/u/proj"))
        self.assertTrue(cn.admitted(c, "x", cwd="/home/u/proj/sub"))
        self.assertFalse(cn.admitted(c, "x", cwd="/home/u/project-other"))

    def test_admission_plugin(self):
        cn.ADMISSION.append(lambda c, r: r.startswith("rev-") or None)
        try:
            self.assertTrue(cn.admitted({}, "rev-1"))
            self.assertFalse(cn.admitted({}, "x", cwd="/"))
        finally:
            cn.ADMISSION.clear()


class SessionCwd(Conf):
    def test_live_session(self):
        d = self.claude / "sessions"
        d.mkdir(parents=True)
        (d / "1.json").write_text(json.dumps({"name": "live", "cwd": "/w/live"}))
        self.assertEqual(cn.session_cwd("live"), "/w/live")

    def test_closed_session_exact_cwd_keeps_dashes(self):
        self.transcript("-home-u-my-proj", "s1", "closed", cwd="/home/u/my-proj")
        self.assertEqual(cn.session_cwd("closed"), "/home/u/my-proj")

    def test_closed_session_without_cwd_record_decodes_dir(self):
        self.transcript("-home-u-proj", "s2", "old")
        self.assertEqual(cn.session_cwd("old"), "/home/u/proj")

    def test_newest_transcript_wins(self):
        self.transcript("-a", "s1", "r", cwd="/a")
        self.transcript("-b", "s2", "r", cwd="/b")
        os.utime(self.claude / "projects" / "-a" / "s1.jsonl", (1, 1))
        self.assertEqual(cn.session_cwd("r"), "/b")

    def test_cached_in_memory_and_on_disk(self):
        self.transcript("-a", "s1", "r", cwd="/a")
        self.assertEqual(cn.session_cwd("r"), "/a")
        (self.claude / "projects" / "-a" / "s1.jsonl").unlink()
        self.assertEqual(cn.session_cwd("r"), "/a", "memory cache")
        cn._cwd_mem.clear()
        self.assertEqual(cn.session_cwd("r"), "/a", "disk cache, shared between processes")

    def test_no_transcript(self):
        self.assertIsNone(cn.session_cwd("nobody"))

    def test_large_transcript_not_loaded(self):
        """Peak memory of a fresh process looking up a closed session with a 120 MB transcript stays far below it."""
        import subprocess
        import sys
        p = self.claude / "projects" / "-big"
        p.mkdir(parents=True)
        chunk = ('{"type":"assistant","text":"' + "x" * 1000 + '"}\n') * 1000
        with open(p / "s3.jsonl", "w") as f:
            f.write(json.dumps({"type": "user", "cwd": "/big"}) + "\n")
            for _ in range(120):
                f.write(chunk)
            f.write(json.dumps({"type": "custom-title", "customTitle": "big"}, separators=(",", ":")) + "\n")
        code = ("import resource, sys; sys.path.insert(0, sys.argv[1]); from aimost import connections as c; "
                "print(c.session_cwd('big'), resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)")
        from helpers import SRC
        out = subprocess.run([sys.executable, "-c", code, SRC], capture_output=True, text=True, env=dict(os.environ)).stdout
        cwd, peak_mb = out.split()
        self.assertEqual(cwd, "/big")
        self.assertLess(int(peak_mb), 60, f"peak {peak_mb} MB with a 120 MB transcript")


class People(Conf):
    def test_owners_and_co_owners_per_role(self):
        c = {"OWNER": "boss", "CO_OWNERS": "deployer:ann, dev*:bob, deployer:boss"}
        self.assertEqual(cn.owners(c, "deployer"), ["boss", "ann"])
        self.assertEqual(cn.owners(c, "devops"), ["boss", "bob"])
        self.assertEqual(cn.owners(c, "other"), ["boss"])

    def test_confirmers(self):
        c = {"OWNER": "boss", "CO_OWNERS": "deployer:ann", "CONFIRMERS": "devops:srv1+srv2,devops:srv3,deployer:ann"}
        self.assertEqual(cn.confirmers(c, "devops"), ["boss", "srv1", "srv2", "srv3"])
        self.assertEqual(cn.confirmers(c, "deployer"), ["boss", "ann"])


class Bots(Conf):
    def setUp(self):
        super().setUp()
        self.conn("w", OWNER="boss", SESSIONS="probe,own,review-1", BOT_SHARED="review-*:review", RELAY="relay")
        for r in ("own", "review", "relay"):
            self.botfile("w", r, f"t-{r}")
        self.c = cn.connections()["w"]

    def test_own_and_shared(self):
        self.assertEqual(cn.bot(self.c, "own"), "t-own")
        self.assertEqual(cn.bot(self.c, "review-1"), "t-review")
        self.assertIsNone(cn.bot(self.c, "probe"))

    def test_relay_only_for_admitted_without_bot(self):
        self.assertEqual(cn.relay(self.c, "probe"), "t-relay")
        self.assertIsNone(cn.relay(self.c, "own"), "a session with its own bot posts with it")
        self.assertIsNone(cn.relay(self.c, "stranger"), "not admitted")
        self.assertIsNone(cn.relay(self.c, "relay"), "not the relay itself")
        self.assertIsNone(cn.bot(self.c, "probe"), "bot() never returns the relay")

    def test_has_bot(self):
        self.assertTrue(cn.has_bot("own"))
        self.assertFalse(cn.has_bot("probe"))

    def test_thread_route(self):
        script = self.tmp / "route.sh"
        script.write_text('#!/bin/sh\n[ "$1" = root1 ] && echo review-1\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        c = dict(self.c, THREAD_ROUTE=f"review={script}")
        self.assertEqual(cn.thread_route(c, "review", "root1"), "review-1")
        self.assertIsNone(cn.thread_route(c, "review", "other-root"))
        self.assertIsNone(cn.thread_route(c, "own", "root1"))


if __name__ == "__main__":
    unittest.main()
