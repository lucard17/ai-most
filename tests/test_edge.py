"""The edge: button presses, owner keys, the "own answer" dialog, redraws (edge spec) — offline, on an in-memory server."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request

from fakemm import FakeMM
from helpers import IsolatedTest

from lmost import buttons, commands, edge as edgemod

SPEC2 = {"context": "c", "parts": [{"title": "p1", "text": "", "options": ["yes", "no"]},
                                   {"title": "p2", "text": "", "options": ["yes", "no"]}]}


class World(IsolatedTest):
    """Owner A (``UA``) with bot ``BA``, owner B (``UB``) with bots ``BB`` and ``BK`` (named "key"), a person ``UH``."""

    def setUp(self):
        super().setUp()
        self.mm = FakeMM()
        mm = self.mm
        mm.user("UA", "alice")
        mm.user("UB", "bob")
        mm.user("UH", "nobots")
        mm.user("BA", "alice-deployer", bot=True, owner="UA")
        mm.user("BB", "bob-checker", bot=True, owner="UB")
        mm.user("BK", "key", bot=True, owner="UB")
        for pid, uid in (("PA", "BA"), ("PB", "BB"), ("PH", "UH")):
            mm.post(pid, uid, {"lmost_question": "ask-1", "lmost_ask": json.loads(json.dumps(SPEC2)), "agent": "deployer"})
        self.lines = []
        self.keys_file = self.tmp / "data" / "edge-keys.json"

    def edge(self, **env) -> edgemod.Edge:
        base = {"EDGE_BOT_TOKEN": "t", "EDGE_PUBLIC": "edge:1", "EDGE_KEYS_FILE": str(self.keys_file),
                "EDGE_MASTER": "m4ster", "EDGE_OWNER_TTL_S": "300"}
        base.update(env)
        return edgemod.Edge(env={k: v for k, v in base.items() if v is not None}, call=self.mm.call,
                            log=self.lines.append)

    def slash(self, e, user, text, name="bot-token"):
        code, r = e.handle(f"/{name}?k={e.slash_key(name)}",
                           f"user_id={user}&user_name={user}&team_id=T&text={text}".encode())
        self.assertEqual(code, 200)
        return r["text"]

    def press(self, e, post, ctx, user="UA"):
        return e.handle_action({"user_id": user, "post_id": post, "trigger_id": "TRG", "context": ctx})

    @staticmethod
    def q1(kid, key, part=1, option=1):
        return {"kid": kid, "secret": key, "q": "ask-1", "part": part, "option": option}

    @staticmethod
    def kv(text, name):
        return next((ln.split("=", 1)[1] for ln in text.splitlines() if ln.startswith(name + "=")), None)


class OwnerKeys(World):
    def issue(self, e, user="UB"):
        out = self.slash(e, user, "bob-checker")
        return self.kv(out, "EDGE_KEY_ID"), self.kv(out, "EDGE_SECRET"), out

    def test_first_key_comes_with_the_first_token(self):
        e = self.edge(EDGE_SECRET="s3")
        kid, key, out = self.issue(e)
        self.assertTrue(kid and key and "TOKEN-VALUE" in out, out)
        self.assertNotIn(key, self.keys_file.read_text())
        self.assertFalse(any(key in ln for ln in self.lines), "no key value in the log")
        self.assertTrue(any(f"key issued, kid {kid}" in ln for ln in self.lines))
        self.assertNotIn("EDGE_SECRET=", self.slash(e, "UB", "bob-checker"), "the second token does not show the key")

    def test_key_works_only_on_its_owners_bots(self):
        e = self.edge(EDGE_SECRET="s3")
        kid, key, _ = self.issue(e)
        self.assertEqual(self.press(e, "PA", self.q1(kid, key))[0], 403, "B's key on A's bot")
        self.assertEqual(self.press(e, "PH", self.q1(kid, key))[0], 403, "B's key on a person's post")
        code, r = self.press(e, "PB", self.q1(kid, key))
        self.assertEqual(code, 200, r)
        ctx2 = [a["integration"]["context"] for att in r["update"]["props"]["attachments"]
                for a in att.get("actions", []) if a["integration"]["context"]["part"] == 2][0]
        self.assertEqual(ctx2["kid"], kid, "redrawn buttons carry the same owner's key")
        self.assertNotIn('"s3"', json.dumps(r), "no shared key in the redrawn post")
        self.assertEqual(r["update"]["props"]["lmost_answers"]["1"]["option"], 1)
        self.assertEqual(r["update"]["props"]["agent"], "deployer")

    def test_shared_key_and_edge_owner(self):
        nk = {"secret": "s3", "q": "ask-1", "part": 1, "option": 1}
        self.assertEqual(self.press(self.edge(EDGE_SECRET="s3"), "PB", nk)[0], 200, "single owner: any bot")
        e2 = self.edge(EDGE_SECRET="s3", EDGE_OWNER="alice")
        self.assertEqual(self.press(e2, "PB", nk)[0], 403, "EDGE_OWNER: not their bot")
        self.assertEqual(self.press(e2, "PA", nk)[0], 200, "EDGE_OWNER: their bot")
        e3 = self.edge(EDGE_SLASH_SECRET="sl4sh", EDGE_OWNER="alice")
        self.assertEqual(self.press(e3, "PA", nk)[0], 403, "no EDGE_SECRET — a button without kid is refused")
        self.assertEqual(self.press(e2, "PA", dict(nk, secret="bad"))[0], 403, "a wrong shared key")

    def test_revocation_holds_for_open_dialogs_and_without_restart(self):
        e = self.edge(EDGE_SECRET="s3")
        kid, key, _ = self.issue(e)
        self.press(e, "PB", {"kid": kid, "secret": key, "q": "ask-1", "part": 1, "custom": 1})
        st = json.loads(self.mm.requests("POST", "/actions/dialogs/open")[-1][2]["dialog"]["state"])
        self.assertEqual(st["kid"], kid)
        self.assertNotIn("secret", st)
        self.assertIn("revoked", self.slash(e, "UB", "key revoke"))
        dlg = {"user_id": "UA", "state": json.dumps(st), "submission": {"answer": "own"}}
        self.assertEqual(e.handle_dialog(dlg)[0], 403, "a dialog opened before the revocation")
        self.assertEqual(self.press(e, "PB", self.q1(kid, key))[0], 403)
        self.assertEqual(self.press(e, "PA", {"secret": "s3", "q": "ask-1", "part": 1, "option": 1})[0], 200)
        out = self.slash(e, "UB", "key")
        kid2, key2 = self.kv(out, "EDGE_KEY_ID"), self.kv(out, "EDGE_SECRET")
        self.assertTrue(kid2 and key2 and key2 != key, out)
        self.assertEqual(self.press(e, "PB", self.q1(kid2, key2))[0], 200)

    def test_who_may_get_a_key(self):
        e = self.edge(EDGE_SECRET="s3")
        self.assertNotIn("EDGE_SECRET=", self.slash(e, "UH", "key"), "a person without bots")
        self.assertNotIn("EDGE_SECRET=", self.slash(e, "BB", "key"), "a bot")
        self.assertIn("not enabled", self.slash(self.edge(EDGE_SECRET="s3", EDGE_MASTER=None), "UB", "key"))

    def test_broken_or_missing_keys_file(self):
        e = self.edge(EDGE_SECRET="s3")
        kid, key, _ = self.issue(e)
        good = self.keys_file.read_text()
        self.keys_file.write_text(good[:20])
        self.assertEqual(self.press(e, "PB", self.q1(kid, key))[0], 403, "the previous version is not used")
        self.assertEqual(self.press(e, "PB", {"secret": "s3", "q": "ask-1", "part": 1, "option": 1})[0], 200)
        self.assertTrue(any("keys file not read" in ln for ln in self.lines))
        self.keys_file.unlink()
        self.assertEqual(self.press(e, "PB", self.q1(kid, key))[0], 403)
        self.keys_file.write_text(good)
        self.assertEqual(self.press(e, "PB", self.q1(kid, key))[0], 200)

    def test_reassigned_bot(self):
        e = self.edge(EDGE_SECRET="s3", EDGE_OWNER_TTL_S="0")
        kid, key, _ = self.issue(e)
        self.mm.bots["BB"] = "UA"
        self.assertEqual(self.press(e, "PB", self.q1(kid, key))[0], 403, "the previous owner's key")

    def test_record_without_owner(self):
        e = self.edge(EDGE_SECRET="s3")
        self.keys_file.parent.mkdir(parents=True)
        self.keys_file.write_text(json.dumps({"kNO": {"gen": 1, "created": 1}}))
        key = e.key_for("kNO", 1)
        self.assertEqual(self.press(e, "PH", self.q1("kNO", key))[0], 403)
        self.assertEqual(self.press(e, "PB", self.q1("kNO", key))[0], 403)

    def test_failed_write_keeps_the_key(self):
        e = self.edge(EDGE_SECRET="s3")
        kid, key, _ = self.issue(e)
        before = self.keys_file.read_bytes()
        tmp = self.keys_file.parent / f"{self.keys_file.name}.{os.getpid()}.tmp"
        tmp.mkdir()
        try:
            out = self.slash(e, "UB", "key revoke")
        finally:
            tmp.rmdir()
        self.assertIn("NOT taken", out)
        self.assertEqual(self.keys_file.read_bytes(), before)
        self.assertEqual(self.press(e, "PB", self.q1(kid, key))[0], 200, "not revoked in memory either")

    def test_revoke_without_bots_and_bot_named_key(self):
        e = self.edge(EDGE_SECRET="s3", EDGE_OWNER_TTL_S="0")
        kid, key, _ = self.issue(e)
        self.mm.bots.update(BB="UA", BK="UA")
        self.assertIn("Key revoked", self.slash(e, "UB", "key revoke"))
        self.mm.bots.update(BB="UB", BK="UB")
        self.assertEqual(self.press(e, "PB", self.q1(kid, key))[0], 403, "a revoked key does not come back")
        self.assertIn("TOKEN-VALUE", self.slash(e, "UB", "@key"))

    def test_typo_in_edge_owner_stops_the_start(self):
        self.assertIn("EDGE_OWNER=alcie not found", self.edge(EDGE_SECRET="s3", EDGE_OWNER="alcie").check())
        self.assertIsNone(self.edge(EDGE_SECRET="s3", EDGE_OWNER="alice").check())
        self.assertIn("EDGE_BOT_TOKEN", self.edge(EDGE_BOT_TOKEN="").check())

    def test_bridge_buttons_carry_the_kid(self):
        auth = buttons.auth_from({"EDGE_SECRET": "kx", "EDGE_KEY_ID": "k123"})
        _, a = buttons.render("ask-1", {"context": "", "parts": [{"title": "t", "text": "", "options": ["yes"]}]}, {},
                              "s", auth, "e")
        self.assertTrue(all(x["integration"]["context"].get("kid") == "k123" for x in a[0]["actions"]))
        _, a = buttons.render_confirm("c1", "x", "", "s", auth, "e")
        self.assertTrue(all(x["integration"]["context"].get("kid") == "k123" for x in a[0]["actions"]))


class Dialog(World):
    def open(self, e, post="PA", user="UA"):
        self.press(e, post, {"secret": "s3", "q": "ask-1", "part": 1, "custom": 1}, user=user)
        return self.mm.requests("POST", "/actions/dialogs/open")[-1][2]["dialog"]

    def test_state_is_signed_and_has_no_key(self):
        e = self.edge(EDGE_SECRET="s3")
        st = json.loads(self.open(e)["state"])
        self.assertNotIn("secret", st)
        forged = dict(st, post_id="PB")
        self.assertEqual(e.handle_dialog({"user_id": "UA", "state": json.dumps(forged),
                                          "submission": {"answer": "x"}})[0], 403)
        self.assertNotIn("lmost_answers", self.mm.posts["PB"]["props"], "PB untouched by the forgery")
        unsigned = {k: v for k, v in st.items() if k != "sig"}
        self.assertEqual(e.handle_dialog({"user_id": "UA", "state": json.dumps(unsigned)})[0], 403)
        old = dict(unsigned, secret="s3")
        self.assertEqual(e.handle_dialog({"user_id": "UA", "state": json.dumps(old)})[0], 403)
        code, r = e.handle_dialog({"user_id": "UA", "state": json.dumps(st), "submission": {"answer": " mine "}})
        self.assertEqual((code, r), (200, {}))
        props = self.mm.posts["PA"]["props"]
        self.assertEqual(props["lmost_answers"]["1"]["text"], "mine")
        self.assertEqual(props["lmost_ask"], SPEC2, "the spec is written back: a patch replaces props whole")
        self.assertIn("Answered 1 of 2", self.mm.posts["PA"]["message"])
        self.assertFalse(any("s3" in ln for ln in self.lines))

    def test_empty_answer_and_withdrawn_question(self):
        e = self.edge(EDGE_SECRET="s3")
        st = self.open(e)["state"]
        self.assertIn("empty", e.handle_dialog({"user_id": "UA", "state": st, "submission": {"answer": " "}})[1]
                      ["errors"]["answer"])
        self.mm.posts["PA"]["props"]["lmost_cancelled"] = {"why": "moot"}
        self.assertIn("withdrawn: moot", e.handle_dialog({"user_id": "UA", "state": st, "submission": {"answer": "x"}})[1]
                      ["errors"]["answer"])

    def test_mobile_hint(self):
        e = self.edge(EDGE_SECRET="s3")
        self.mm.posts["PA"]["props"]["lmost_ask"]["parts"][0]["text"] = "why it matters"
        code, r = self.press(e, "PA", {"secret": "s3", "q": "ask-1", "part": 1, "custom": 1})
        dlg = self.mm.requests("POST", "/actions/dialogs/open")[-1][2]["dialog"]
        self.assertTrue(dlg["introduction_text"].startswith("why it matters"))
        self.assertIn("answer in words in the question's thread", dlg["introduction_text"])
        self.assertEqual(r["ephemeral_text"], edgemod.MOBILE_HINT)

    def test_post_without_spec_asks_to_press_again(self):
        e = self.edge(EDGE_SECRET="s3")
        self.mm.post("PX", "BA", {})
        code, r = self.press(e, "PX", {"secret": "s3", "q": "ask-1", "part": 1, "option": 1})
        self.assertEqual((code, r["ephemeral_text"]), (200, edgemod.RETRY))


class Addressees(World):
    def test_only_addressees_press(self):
        e = self.edge(EDGE_SECRET="s3")
        self.mm.posts["PA"]["props"]["lmost_ask"]["to"] = ["bob"]
        code, r = self.press(e, "PA", {"secret": "s3", "q": "ask-1", "part": 1, "option": 1}, user="UA")
        self.assertIn("only from the addressee", r["ephemeral_text"])
        self.assertNotIn("update", r)
        code, r = self.press(e, "PA", {"secret": "s3", "q": "ask-1", "part": 1, "option": 2}, user="UB")
        self.assertEqual(r["update"]["props"]["lmost_answers"]["1"]["by"], "bob")

    def test_solved_question_reads_question_to_answer(self):
        e = self.edge(EDGE_SECRET="s3")
        self.mm.posts["PA"]["props"]["lmost_answers"] = {"1": {"option": 1, "by": "alice"}}
        code, r = self.press(e, "PA", {"secret": "s3", "q": "ask-1", "part": 2, "option": 2})
        self.assertEqual(r["update"]["message"], "✅ c\np1 → **yes** — @alice\np2 → **no** — @alice")

    def test_confirm_only_by_allowed(self):
        e = self.edge(EDGE_SECRET="s3")
        ctx = {"kind": "confirm", "id": "c1", "action": "deploy", "details": "", "agent": "deployer", "secret": "s3",
               "to": ["alice", "bob"], "answer": "yes"}
        r = self.press(e, "PA", ctx, user="UH")[1]
        self.assertIn("Confirmed by @alice or @bob", r["ephemeral_text"])
        r = self.press(e, "PA", ctx, user="UB")[1]
        self.assertEqual(r["update"]["props"]["lmost_confirm"]["by"], "bob")
        self.assertIn("✅ confirmed — @bob", r["update"]["message"])
        self.assertEqual(r["update"]["props"]["attachments"], [])

    def test_withdrawn_question_is_not_answered(self):
        e = self.edge(EDGE_SECRET="s3")
        self.mm.posts["PA"]["props"]["lmost_cancelled"] = {"why": "done elsewhere"}
        r = self.press(e, "PA", {"secret": "s3", "q": "ask-1", "part": 1, "option": 1})[1]
        self.assertIn("withdrawn: done elsewhere", r["ephemeral_text"])


class Marks(World):
    def test_discuss_waiting_drop_and_snooze(self):
        e = self.edge(EDGE_SECRET="s3")
        self.mm.posts["PA"]["props"]["keep"] = "x"
        r = self.press(e, "PA", {"kind": "discuss", "q": "ask-1", "secret": "s3"})[1]
        self.assertIn("opens in your DM", r["ephemeral_text"])
        props = self.mm.posts["PA"]["props"]
        self.assertEqual(props["lmost_discuss"]["UA"]["by"], "alice")
        self.assertEqual(props["keep"], "x", "other props kept")
        self.assertIn("already open", self.press(e, "PA", {"kind": "discuss", "q": "ask-1", "secret": "s3"})[1]
                      ["ephemeral_text"])
        # a later answer keeps the discussion marks
        r = self.press(e, "PA", {"secret": "s3", "q": "ask-1", "part": 1, "option": 1})[1]
        self.assertIn("UA", r["update"]["props"]["lmost_discuss"])
        self.press(e, "PA", {"kind": "waiting-drop", "q": "ask-9", "asker": "devops", "secret": "s3"})
        self.assertEqual(self.mm.posts["PA"]["props"]["lmost_waiting_drop"]["ask-9"]["asker"], "devops")
        self.assertIn("No period", self.press(e, "PA", {"kind": "waiting-snooze", "secret": "s3"})[1]["ephemeral_text"])
        r = self.press(e, "PA", {"kind": "waiting-snooze", "key": "ask-9", "selected_option": "2h", "secret": "s3"})[1]
        self.assertTrue(r["ephemeral_text"].startswith("Snoozing"))
        self.assertEqual(self.mm.posts["PA"]["props"]["lmost_waiting_snooze"]["ask-9"]["value"], "2h")


class Http(World):
    """The HTTP layer: slash URL keys, unknown paths, the version, and the key kept out of the access log."""

    def test_server(self):
        e = self.edge(EDGE_SECRET="s3", EDGE_LISTEN="127.0.0.1:0")
        srv = edgemod.make_server(e, reload_s=0)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        def post(path, data=b""):
            try:
                with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data)) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as x:
                x.close()
                return x.code, None
        try:
            self.assertEqual(post("/unfollow?k=wrong", b"user_id=UA&text=")[0], 403)
            self.assertEqual(post("/unfollow", b"user_id=UA&text=")[0], 403)
            code, r = post(f"/unfollow?k={e.slash_key('unfollow')}", b"user_id=UA&text=")
            self.assertEqual((code, r["response_type"]), (200, "ephemeral"))
            self.assertIn("No auto-unfollow", r["text"])
            self.assertEqual(post("/nothing", b"{}")[0], 404)
            self.assertEqual(post("/action", b"{}")[0], 403)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/version") as r:
                self.assertEqual(json.loads(r.read())["version"], edgemod.__version__)
            self.assertFalse(any(e.slash_key("unfollow") in ln for ln in self.lines), "slash key not in the log")
            self.assertTrue(any("?k=…" in ln for ln in self.lines))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_handler_error_is_generic(self):
        e = self.edge(EDGE_SECRET="s3")
        self.mm.fail[r"/preferences/lmost_unfollow$"] = 500
        text = self.slash(e, "UA", "", name="unfollow")
        self.assertEqual(text, commands.FAILED)
        self.assertTrue(any("unfollow: command failed: HTTPError" in ln for ln in self.lines))


class Reload(IsolatedTest):
    """A changed package is picked up by re-executing in place; code that does not compile is not run."""

    def test_reexec_on_change(self):
        import shutil
        import socket
        import subprocess
        import sys
        import time
        from helpers import SRC
        pkg = self.tmp / "pkg"
        shutil.copytree(os.path.join(SRC, "lmost"), pkg / "lmost", ignore=shutil.ignore_patterns("__pycache__"))
        with socket.socket() as so:
            so.bind(("127.0.0.1", 0))
            port = so.getsockname()[1]
        log = open(self.tmp / "edge.log", "w")
        env = dict(os.environ, PYTHONPATH=str(pkg), EDGE_BOT_TOKEN="t", EDGE_SECRET="s", EDGE_LISTEN=f"127.0.0.1:{port}",
                   EDGE_RELOAD_S="0.1", EDGE_UNFOLLOW_EVERY_S="0", PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.Popen([sys.executable, "-m", "lmost", "edge", "serve"], env=env, stderr=log, cwd=str(self.tmp))

        def version(want=None, timeout=10.0):
            end, got = time.time() + timeout, None
            while time.time() < end:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/version", timeout=1) as r:
                        got = json.loads(r.read())["version"]
                        if want is None or got == want:
                            return got
                except OSError:
                    pass
                time.sleep(0.05)
            return got

        def set_version(v):
            init = pkg / "lmost" / "__init__.py"
            init.write_text(init.read_text().split("__version__")[0] + f'__version__ = "{v}"\n')
        try:
            first = version()
            self.assertEqual(first, edgemod.__version__)
            set_version("9.9.9")
            self.assertEqual(version("9.9.9"), "9.9.9")
            self.assertIsNone(p.poll(), "the same process (execv)")
            ed = pkg / "lmost" / "edge.py"
            good = ed.read_text()
            ed.write_text(good + "\ndef broken(:\n")
            set_version("9.9.10")
            time.sleep(0.6)
            self.assertEqual(version(), "9.9.9", "broken code is not run")
            ed.write_text(good)
            self.assertEqual(version("9.9.10"), "9.9.10", "fixed code is run")
        finally:
            p.terminate()
            p.wait(5)
            log.close()
        text = (self.tmp / "edge.log").read_text()
        self.assertEqual(text.count("does not compile"), 1, text)
        self.assertEqual(text.count("re-executing"), 2, text)


if __name__ == "__main__":
    import unittest
    unittest.main()
