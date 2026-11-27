"""Built-in GitLab plugin (gitlab-mr spec) against a fake ``glab`` — never a real GitLab."""

from __future__ import annotations

import json
import os
import stat
import sys
import time

from helpers import IsolatedTest

from lmost import inbox, plugins, textcheck
from lmost_gitlab import glab, hook, mrlinks, watch

FAKE = r'''#!%s
import json, os, sys
db = json.load(open(os.environ["FAKE_GLAB_DB"]))
with open(os.environ["FAKE_GLAB_DB"] + ".log", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
if db.get("_down"):
    sys.stderr.write("dial tcp: connection refused\n"); sys.exit(1)
path = sys.argv[2]
if path not in db:
    sys.stderr.write("HTTP 404 Not Found\n"); sys.exit(1)
v = db[path]
if isinstance(v, dict) and "_status" in v:
    sys.stderr.write("HTTP %%d\n" %% v["_status"]); sys.exit(1)
print(json.dumps(v))
''' % sys.executable

MR = "projects/grp%2Fbackend/merge_requests/12"


class Glab(IsolatedTest):
    """A fake glab answering from a JSON file; every call is logged."""

    def setUp(self):
        super().setUp()
        self._saved_env = {k: os.environ.get(k) for k in ("LMOST_GLAB_BIN", "FAKE_GLAB_DB", "LMOST_GITLAB_GROUP")}
        b = self.tmp / "glab"
        b.write_text(FAKE)
        b.chmod(b.stat().st_mode | stat.S_IXUSR)
        self.db_file = self.tmp / "glab.json"
        self.db = {}
        self.flush()
        os.environ.update({"LMOST_GLAB_BIN": str(b), "FAKE_GLAB_DB": str(self.db_file)})

    def tearDown(self):
        for k, v in self._saved_env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        super().tearDown()

    def flush(self):
        self.db_file.write_text(json.dumps(self.db))

    def calls(self):
        p = self.db_file.with_name("glab.json.log")
        return p.read_text().splitlines() if p.exists() else []


class Links(Glab):
    def setUp(self):
        super().setUp()
        self.db[MR] = {"title": "feat:  unit  by scheme", "web_url": "https://gl.example/grp/backend/-/merge_requests/12"}
        self.flush()

    def draft(self, text, group="grp"):
        d = textcheck.Draft(text, conn={"GITLAB_GROUP": group} if group else {})
        mrlinks.check(d)
        return d

    def test_ref_becomes_link_with_title(self):
        d = self.draft("merged backend!12 to prod")
        self.assertEqual(d.message, "merged [backend!12](https://gl.example/grp/backend/-/merge_requests/12) "
                                    "«feat: unit by scheme» to prod")

    def test_repo_word_before_number(self):
        self.assertIn("[backend!12](", self.draft("merged backend !12 to prod").message)

    def test_link_gets_title_once(self):
        d = self.draft("see [backend!12](https://x/12)")
        self.assertEqual(d.message, "see [backend!12](https://x/12) «feat: unit by scheme»")
        d = self.draft("**feat: unit by scheme** — [backend!12](https://x/12)")
        self.assertEqual(d.message, "**feat: unit by scheme** — [backend!12](https://x/12)")

    def test_code_untouched_and_not_mrs(self):
        d = self.draft("`backend!12` and ```\nbackend!12\n``` wow!!! ![img](x.png), 3!=4, other!5")
        self.assertEqual(d.message, "`backend!12` and ```\nbackend!12\n``` wow!!! ![img](x.png), 3!=4, other!5")

    def test_bare_number_refused_20(self):
        with self.assertRaises(textcheck.Refused) as e:
            self.draft("merged !12 today")
        self.assertEqual(e.exception.code, 20)
        self.assertIn("<repo>!12", e.exception.hint)
        with self.assertRaises(textcheck.Refused):
            self.draft("merged foo !12")          # the word is not a repository (404) — a bare number

    def test_gitlab_down_text_as_is(self):
        self.db["_down"] = True
        self.flush()
        d = self.draft("merged backend !12 and api!3")
        self.assertEqual(d.message, "merged backend !12 and api!3")
        self.assertTrue(any("GitLab unreachable" in n for n in d.notes))
        self.assertEqual(len(self.calls()), 1, "the first failure stops further lookups")

    def test_question_parts_rewritten(self):
        d = textcheck.Draft("", ask={"context": "merge backend!12?", "parts": [{"title": "now?", "text": "backend!12"}]},
                            conn={"GITLAB_GROUP": "grp"})
        mrlinks.check(d)
        self.assertIn("«feat: unit by scheme»", d.ask["context"])
        self.assertIn("[backend!12](", d.ask["parts"][0]["text"])

    def test_no_glab_nothing(self):
        os.environ["LMOST_GLAB_BIN"] = str(self.tmp / "missing")
        d = self.draft("merged !12")
        self.assertEqual(d.message, "merged !12")


class Watch(Glab):
    ROLE = "author"

    def setUp(self):
        super().setUp()
        self.db.update({
            "user": {"username": "me"},
            MR: {"title": "T", "web_url": "https://gl.example/grp/backend/-/merge_requests/12", "state": "opened",
                 "sha": "aaa", "head_pipeline": {"status": "running", "sha": "aaa"}},
            MR + "/approvals": {"approved_by": []},
            MR + "/notes?sort=desc&order_by=created_at&per_page=100&page=1": [
                {"id": 5, "body": "old", "author": {"username": "bob"}}],
        })
        self.flush()

    def box(self):
        return [(e["from"], e["text"], e.get("role"), e["kind"]) for _, e in inbox.Inbox(self.ROLE).unread()]

    def test_before_link_not_delivered_then_events_then_merged(self):
        ref = watch.add("https://gl.example/grp/backend/-/merge_requests/12", self.ROLE)
        self.assertEqual(ref, "backend!12")
        self.assertEqual(self.box(), [], "what happened before linking does not come")
        self.db[MR + "/approvals"] = {"approved_by": [{"user": {"username": "ann"}}]}
        self.db[MR + "/notes?sort=desc&order_by=created_at&per_page=100&page=1"] = [
            {"id": 8, "body": "mine", "author": {"username": "me"}},
            {"id": 7, "body": "merged by bot", "system": True, "author": {"username": "ann"}},
            {"id": 6, "body": "please rename", "author": {"username": "bob"}, "position": {"new_line": 3}},
            {"id": 5, "body": "old", "author": {"username": "bob"}}]
        self.db[MR].update(head_pipeline={"status": "failed", "sha": "aaa", "web_url": "https://p/1"})
        self.flush()
        self.assertEqual(watch.once(), 0)
        got = self.box()
        texts = [t for _, t, _, _ in got]
        self.assertEqual(len(got), 3, texts)
        self.assertTrue(texts[0].startswith("✅ @ann approved [backend!12]"))
        self.assertIn("💬 @bob on [backend!12](https://gl.example/grp/backend/-/merge_requests/12) «T» (on a line of code): "
                      "please rename", texts[1])
        self.assertIn("🔴 pipeline failed", texts[2])
        self.assertEqual({(r, k) for _, _, r, k in got}, {("peer", "gitlab")}, "a source writes as peer")
        inbox.Inbox(self.ROLE).unread(ack=True)

        watch.once()
        self.assertEqual(self.box(), [], "nothing new — nothing; the failed sha is not repeated")
        self.db[MR + "/approvals"] = {"approved_by": []}
        self.db[MR].update(state="merged", merged_by={"username": "ann"},
                           head_pipeline={"status": "success", "sha": "aaa"})
        self.flush()
        watch.once()
        texts = [t for _, t, _, _ in self.box()]
        self.assertEqual(len(texts), 2, texts)
        self.assertTrue(texts[0].startswith("↩️ @ann removed the approval"))
        self.assertEqual(texts[1], "🏁 [backend!12](https://gl.example/grp/backend/-/merge_requests/12) «T» — merged (@ann)")
        self.assertEqual(watch.links(), {}, "merged — unlinked")

    def test_ref_needs_group(self):
        with self.assertRaises(watch.WatchError):
            watch.add("backend!12", self.ROLE)
        os.environ["LMOST_GITLAB_GROUP"] = "grp"
        self.assertEqual(watch.add("backend!12", self.ROLE), "backend!12")
        self.assertEqual(watch.links()["backend!12"]["project"], "grp/backend")

    def test_gone_unlinks_with_a_notice(self):
        watch.add("https://gl.example/grp/backend/-/merge_requests/12", self.ROLE)
        self.db[MR] = {"_status": 403}
        self.flush()
        watch.once()
        self.assertIn("cannot be opened any more", self.box()[0][1])
        self.assertEqual(watch.links(), {})

    def test_owner_source_by_connection(self):
        c = self.tmp / "config" / "connections"
        c.mkdir(parents=True)
        (c / "it.env").write_text(f"URL=http://127.0.0.1:9\nOWNER=o\nSESSIONS={self.ROLE}\nSOURCES_OWNER=gitlab\n")
        watch.inbox_put(self.ROLE, "ann", "x")
        self.assertEqual(self.box()[0][2], "owner")

    def test_switched_off_not_polled(self):
        watch.add("https://gl.example/grp/backend/-/merge_requests/12", self.ROLE)
        c = self.tmp / "config" / "connections"
        c.mkdir(parents=True)
        (c / "it.env").write_text(f"URL=http://127.0.0.1:9\nOWNER=o\nSESSIONS={self.ROLE}\nPLUGINS_OFF=gitlab\n")
        n = len(self.calls())
        self.db[MR + "/approvals"] = {"approved_by": [{"user": {"username": "ann"}}]}
        self.flush()
        watch.once()
        self.assertEqual(self.box(), [])
        self.assertEqual(len(self.calls()), n, "not asked at all")

    def test_cli_once_without_glab_is_silent(self):
        r = self.lmost("gitlab", "once", env={"LMOST_GLAB_BIN": str(self.tmp / "missing")})
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


class Hook(Glab):
    def setUp(self):
        super().setUp()
        paths_inbox = self.tmp / "state" / "inbox"
        paths_inbox.mkdir(parents=True)
        os.environ["LMOST_ROLE"] = "author"
        self.spawned = []

    def run_hook(self, cmd, out, **extra):
        inp = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": cmd},
               "tool_response": {"stdout": out, "stderr": ""}, **extra}
        return hook.handle(json.dumps(inp), spawn=self.spawned.append)

    URL = "https://gl.example/grp/backend/-/merge_requests/12"

    def test_glab_mr_create_links(self):
        self.assertEqual(self.run_hook("cd repo && glab mr create --fill", f"Created {self.URL}\n"), self.URL)
        self.assertEqual(self.spawned, [["gitlab", "add", self.URL, "--session", "author"]])

    def test_wrapper_by_pattern(self):
        self.assertIsNone(self.run_hook("my-mr --title x", self.URL))
        os.environ["LMOST_MR_CREATE_PATTERNS"] = "my-mr"
        self.addCleanup(os.environ.pop, "LMOST_MR_CREATE_PATTERNS", None)
        self.assertEqual(self.run_hook("FOO=1 ./bin/my-mr --title x", self.URL), self.URL)

    def test_success_is_the_url_not_the_exit_code(self):
        self.assertIsNone(self.run_hook("glab mr create --fill", "error: no remote"))
        self.assertIsNone(self.run_hook("echo glab mr create", self.URL), "not in command position")
        self.assertEqual(self.spawned, [])

    def test_silent_without_chat_or_glab(self):
        os.rmdir(self.tmp / "state" / "inbox")
        self.assertIsNone(self.run_hook("glab mr create", self.URL))
        (self.tmp / "state" / "inbox").mkdir()
        os.environ["LMOST_GLAB_BIN"] = str(self.tmp / "missing")
        self.assertIsNone(self.run_hook("glab mr create", self.URL))

    def test_cli_hook_spawns_add_in_background(self):
        self.db.update({"user": {"username": "me"},
                        MR: {"title": "T", "web_url": self.URL, "state": "opened"},
                        MR + "/approvals": {"approved_by": []},
                        MR + "/notes?sort=desc&order_by=created_at&per_page=100&page=1": []})
        self.flush()
        inp = json.dumps({"session_id": "s1", "tool_input": {"command": "glab mr create"},
                          "tool_response": {"stdout": self.URL}})
        import subprocess
        e = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src"))
        p = subprocess.run([sys.executable, "-m", "lmost", "gitlab", "hook"], input=inp, capture_output=True,
                           text=True, env=e)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))
        for _ in range(50):
            if watch.links().get("backend!12", {}).get("seen"):
                break
            time.sleep(0.1)
        self.assertEqual(watch.links()["backend!12"]["session"], "author")


class Who(Glab):
    def test_search(self):
        self.db["users?search=ann%20lee&per_page=20"] = [{"username": "alee", "name": "Ann Lee"}]
        self.flush()
        from lmost_gitlab import who
        self.assertEqual(who.search("ann lee"), [{"source": "GitLab", "login": "alee", "name": "Ann Lee"}])


class Available(IsolatedTest):
    def test_glab_needs_a_config_unless_given(self):
        saved = os.environ.pop("LMOST_GLAB_BIN", None)
        try:
            bindir = self.tmp / "bin"
            bindir.mkdir()
            (bindir / "glab").write_text("#!/bin/sh\n")
            (bindir / "glab").chmod(0o755)
            path = os.environ["PATH"]
            os.environ["PATH"] = f"{bindir}:{path}"
            try:
                self.assertFalse(glab.available(), "no ~/.config/glab-cli/config.yml — silent")
                cfg = self.tmp / "home" / ".config" / "glab-cli"
                cfg.mkdir(parents=True)
                (cfg / "config.yml").write_text("hosts: {}\n")
                self.assertTrue(glab.available())
            finally:
                os.environ["PATH"] = path
        finally:
            if saved is not None:
                os.environ["LMOST_GLAB_BIN"] = saved


class Timer(Glab):
    """`lmost setup` gets the MR-watch timer from the plugin only where glab can be used."""

    def units(self):
        from lmost import cli_who, install
        saved, loaded, pl = list(install.SERVICES), plugins._loaded, dict(plugins._plugins)
        tc, ws = list(textcheck.TEXT_CHECKS), list(cli_who.WHO_SOURCES)
        try:
            install.SERVICES.clear()
            plugins._plugins.clear()
            plugins._loaded = False
            plugins.load()
            return sorted(n for n, _ in install.SERVICES)
        finally:
            install.SERVICES[:] = saved
            textcheck.TEXT_CHECKS[:], cli_who.WHO_SOURCES[:] = tc, ws
            plugins._plugins.clear()
            plugins._plugins.update(pl)
            plugins._loaded = loaded

    def test_timer_only_with_glab(self):
        self.assertEqual(self.units(), ["lmost-gitlab.service", "lmost-gitlab.timer"])
        os.environ["LMOST_GLAB_BIN"] = str(self.tmp / "no-such-glab")
        self.assertEqual(self.units(), [])
