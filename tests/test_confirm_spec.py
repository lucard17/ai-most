"""A confirmation describes the action by fields (authority spec, "a confirmation describes the action"): the description
check, the post's template, merge request cards, which merge a command merges, and the CLI. Oracle: upstream
chat-confirm-template.test.sh, chat-confirm.test.sh (confirm-announce-templates)."""

from __future__ import annotations

import json
import subprocess
import unittest

from helpers import IsolatedTest

from aimost import confirm, confirm_spec

GOOD = {"do": "Deploy api v1.2 to the web hosts", "why": "the owner asked in the release thread",
        "steps": ["the new image starts on web-1 and web-2", "the old one stops after the health check"],
        "keeps": "the database and its schema", "rollback": "redeploy v1.1, five minutes",
        "mrs": ["api!12"], "merges": ["api!13"], "commands": ["ansible-playbook deploy.yml -l web"]}


class Check(unittest.TestCase):
    def errs(self, **kw):
        return confirm_spec.check(dict(GOOD, **kw))[1]

    def test_good_is_normalized(self):
        out, err = confirm_spec.check(dict(GOOD, do="  Deploy   api\nv1.2 "))
        self.assertEqual(err, [])
        self.assertEqual(out["do"], "Deploy api v1.2")
        self.assertEqual(out["mrs"], ["api!12", "api!13"], "a merged one is also one the action is about")
        self.assertEqual(out["merges"], ["api!13"])
        self.assertEqual(out["commands"], ["ansible-playbook deploy.yml -l web"])

    def test_required_fields(self):
        _, err = confirm_spec.check({"do": "x"})
        self.assertEqual([e.split(" —")[0] for e in err if e.startswith("no field")],
                         ["no field why", "no field keeps", "no field rollback", "no field steps"])
        self.assertIn("irreversible", next(e for e in err if "rollback" in e))
        self.assertEqual(confirm_spec.check([1])[0], None)

    def test_limits(self):
        self.assertIn("do: 161 characters, limit 160", self.errs(do="x" * 161))
        self.assertIn("steps: 8 steps, limit 7", self.errs(steps=["a step"] * 8))
        self.assertIn("steps[1]: 201 characters, limit 200", self.errs(steps=["x" * 201]))
        self.assertTrue(any("limit 10" in e for e in self.errs(mrs=[f"api!{i}" for i in range(11)])))

    def test_a_command_in_a_meaning_field(self):
        for field, v in (("do", "glab mr merge 467 --yes"), ("why", "see && make deploy"), ("keeps", "run with --force"),
                         ("rollback", "`git revert HEAD`"), ("do", "restart; systemctl restart nginx")):
            e = self.errs(**{field: v})
            self.assertTrue(any(x.startswith(f"{field}: a command") for x in e), (field, v, e))
        self.assertTrue(any(x.startswith("steps[2]: a command") for x in self.errs(steps=["fine", "ssh web-1 reboot"])))
        for v in ("merging through glab", "the plan stage closes", "rights are untouched; sessions get the fix",
                  "`bin/deploy.sh` changes", "a git hook", "node 20 stays"):
            self.assertEqual(self.errs(do=v), [], v)

    def test_types_and_refs(self):
        self.assertIn("steps: a list of strings expected, not int", self.errs(steps=3))
        self.assertEqual(confirm_spec.check(dict(GOOD, steps="one step"))[0]["steps"], ["one step"])
        self.assertEqual(confirm_spec.check(dict(GOOD, mrs="api!12"))[0]["mrs"][0], "api!12", "a string is one item")
        self.assertTrue(any("<repo>!<N>" in e for e in self.errs(mrs=["api 12"])))
        self.assertEqual(self.errs(mrs=["web#7"]), [], "a GitHub pull request")
        self.assertIn("unknown fields: shell", self.errs(shell="x"))


class Template(unittest.TestCase):
    def post(self, spec=None, cards=()):
        out, err = confirm_spec.check(spec or GOOD)
        self.assertEqual(err, [])
        return confirm.render_confirm_spec("c1", out, list(cards), "deployer", "http://edge:8767/action",
                                           {"secret": "s"}, to=["owner"])

    def test_fields_in_order_commands_not_posted(self):
        msg, atts = self.post(cards=[{"ref": "api!12", "url": "https://git.example/api/-/merge_requests/12",
                                      "title": "Retry the upload", "state": "open", "sha": "abcd1234", "ci": "success"},
                                     {"ref": "api!13", "title": None}])
        self.assertEqual(msg.split("\n"), [
            "🛡 **Confirm: Deploy api v1.2 to the web hosts**",
            "the owner asked in the release thread",
            "",
            "- **MR:** [api!12](https://git.example/api/-/merge_requests/12) «Retry the upload» · open · CI on `abcd1234` — "
            "success",
            "- **MR:** api!13 — not checked (no answer about it)",
            "",
            "**What happens:**",
            "1. the new image starts on web-1 and web-2",
            "2. the old one stops after the health check",
            "",
            "**Stays the same:** the database and its schema",
            "**Rollback:** redeploy v1.1, five minutes",
            "_Merge of api!13 — once each; anything else — one “yes”, one action; 15 minutes._"])
        self.assertNotIn("ansible-playbook", msg)
        ctx = atts[0]["actions"][0]["integration"]["context"]
        self.assertEqual((ctx["kind"], ctx["v"], ctx["id"], ctx["answer"], ctx["to"]), ("confirm", 2, "c1", "yes", ["owner"]))
        self.assertNotIn("action", ctx, "the text is in the post, not in the button")

    def test_without_merges_the_plain_rule(self):
        msg, _ = self.post(dict(GOOD, mrs=[], merges=[]))
        self.assertTrue(msg.endswith("\n_One “yes” — one action, 15 minutes._"))
        self.assertNotIn("**MR:**", msg)

    def test_done_keeps_what_was_seen(self):
        msg, _ = self.post()
        done = confirm.confirm_done("@owner @bob\n" + msg, {"answer": "yes", "by": "bob"})
        self.assertTrue(done.startswith("@owner @bob\n🛡 **Confirm: Deploy api"))
        self.assertTrue(done.endswith("**Rollback:** redeploy v1.1, five minutes\n\n✅ confirmed — @bob"))
        self.assertEqual(confirm.confirm_done("one line", {"answer": "no", "by": "x"}), "one line\n\n❌ declined — @x")


class Cards(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.saved = list(confirm.CARDS)
        self.addCleanup(lambda: confirm.CARDS.__setitem__(slice(None), self.saved))
        confirm.CARDS[:] = []

    def test_first_card_wins_failure_is_not_checked(self):
        def boom(ref, conn):
            raise RuntimeError("down")
        confirm.CARDS[:] = [boom, lambda ref, conn: None, lambda ref, conn: {"title": "T", "state": "open"},
                            lambda ref, conn: {"title": "never"}]
        cards = confirm.cards_for({"mrs": ["api!1"], "merges": []}, {})
        self.assertEqual(cards, [{"title": "T", "state": "open", "ref": "api!1"}])
        confirm.CARDS[:] = [boom]
        self.assertEqual(confirm.cards_for({"mrs": ["api!1"]}, {}), [{"title": None, "ref": "api!1"}])

    def test_nothing_to_merge(self):
        confirm.CARDS[:] = [lambda ref, conn: {"title": "T", "state": "merged"}]
        with self.assertRaises(confirm.ConfirmError) as e:
            confirm.cards_for({"mrs": ["api!1"], "merges": ["api!1"]}, {})
        self.assertEqual(e.exception.code, 4)
        self.assertIn("api!1 is already merged", str(e.exception))
        self.assertEqual(confirm.cards_for({"mrs": ["api!1"], "merges": []}, {})[0]["state"], "merged",
                         "about a merged one (a deploy, closing a review) — a card, not a refusal")


class MergeRef(IsolatedTest):
    def test_forms(self):
        self.assertEqual(confirm.merge_ref("glab mr merge 46 --repo group/api --yes"), "api!46")
        self.assertEqual(confirm.merge_ref("glab mr merge 46 -R group/sub/api"), "api!46")
        self.assertEqual(confirm.merge_ref("glab mr merge --repo=group/api 46"), "api!46")
        self.assertEqual(confirm.merge_ref("gh pr merge 7 -R owner/web --squash"), "web#7")
        self.assertEqual(confirm.merge_ref("echo hi && glab mr merge 3 -R g/x; ls"), "x!3")
        self.assertIsNone(confirm.merge_ref("glab mr merge 1 2 -R g/x"), "two numbers — cannot tell")
        self.assertIsNone(confirm.merge_ref("glab mr merge -R g/x"))
        self.assertIsNone(confirm.merge_ref("git merge main"))

    def test_repository_from_the_directory(self):
        repo = self.tmp / "work" / "api"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@git.example:group/api.git"], check=True)
        self.assertEqual(confirm.merge_ref("glab mr merge 9", cwd=str(repo)), "api!9")
        self.assertEqual(confirm.merge_ref(f"cd {repo} && glab mr merge 9", cwd="/"), "api!9")
        self.assertEqual(confirm.merge_ref("cd api && glab mr merge 9", cwd=str(repo.parent)), "api!9")
        self.assertIsNone(confirm.merge_ref("glab mr merge 9", cwd=str(self.tmp)))


class Cli(IsolatedTest):
    def test_first_form_refused(self):
        r = self.aimost("confirm", "deploy api", "v1.2", env={"AIMOST_ROLE": "deployer"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("no longer accepted", r.stderr)
        self.assertIn("--spec", r.stderr)
        r = self.aimost("confirm", env={"AIMOST_ROLE": "deployer"})
        self.assertEqual(r.returncode, 2)

    def test_description_not_in_form_no_post(self):
        f = self.tmp / "a.json"
        f.write_text(json.dumps(dict(GOOD, do="glab mr merge 46 --yes", keeps="")))
        r = self.aimost("confirm", "--spec", str(f), env={"AIMOST_ROLE": "deployer"})
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("nothing posted", r.stderr)
        self.assertIn("do: a command «glab mr merge 46 --yes»", r.stderr)
        self.assertIn("no field keeps", r.stderr)
        r = self.aimost("confirm", "--spec", str(self.tmp / "none.json"), env={"AIMOST_ROLE": "deployer"})
        self.assertEqual(r.returncode, 4)
        r = self.aimost("confirm", "--spec", str(f), "extra", env={"AIMOST_ROLE": "deployer"})
        self.assertEqual((r.returncode, "takes no positional" in r.stderr), (2, True))

    def test_stdin_and_not_admitted(self):
        import os
        import sys
        e = dict(os.environ, PYTHONPATH=__import__("helpers").SRC, AIMOST_ROLE="stranger")
        r = subprocess.run([sys.executable, "-m", "aimost", "confirm", "--spec", "-"], input=json.dumps(GOOD),
                           capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 3, r.stderr)


if __name__ == "__main__":
    unittest.main()
