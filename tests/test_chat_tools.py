"""``aimost react|thread|file|channel|who`` against the disposable real Mattermost."""

from __future__ import annotations

import unittest
from pathlib import Path
import uuid

from itenv import IT, requires_mattermost
from test_post import World, new_channel, owner_call

from aimost import cli_who, paths


@requires_mattermost
class React(World):
    def test_receipt_and_silent_failures(self):
        pid = self.post_id(self.ok("hello"))
        r = self.aimost("react", "it", "deployer", pid, "eyes")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = owner_call("GET", f"/posts/{pid}")["metadata"]["reactions"]
        self.assertEqual([(x["emoji_name"], x["user_id"]) for x in got], [("eyes", IT["AIMOST_IT_BOT_ID_DEPLOYER"])])
        # a bot token for a role the connection does not admit
        (paths.config_dir() / "bots" / "it" / "review").write_text(IT["AIMOST_IT_BOT_REVIEW"])
        public = self.post_id(self.ok("--channel", "agents", "in a channel the review bot is in"))
        for args in (("nope", "deployer", pid, "eyes"), ("it", "stranger", pid, "eyes"),
                     ("it", "review", public, "eyes"), ("it", "deployer", "x" * 26, "eyes")):
            r = self.aimost("react", *args)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (1, "", ""), args)


@requires_mattermost
class Thread(World):
    def test_read_thread_last(self):
        root = self.post_id(self.ok("--new-thread", "root post"))
        owner_call("POST", "/posts", {"channel_id": owner_call("GET", f"/posts/{root}")["channel_id"],
                                      "root_id": root, "message": "owner reply " + "x" * 1600})
        r = self.aimost("thread", root)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertTrue(lines[0].endswith("@deployer: root post"))
        self.assertIn("@owner: owner reply", lines[1])
        self.assertIn("… (+", lines[1])
        r = self.aimost("thread", "--last", "1", root)
        self.assertTrue(r.stdout.startswith("… 1 earlier posts skipped"))

    def test_relay_does_not_read(self):
        r = self.aimost("thread", "x" * 26, env={"AIMOST_ROLE": "probe"})
        self.assertEqual(r.returncode, 3, r.stderr)


@requires_mattermost
class File(World):
    def test_download(self):
        src = self.tmp / "data file.txt"
        src.write_bytes(b"payload-" + uuid.uuid4().bytes)
        pid = self.post_id(self.ok("--file", str(src), "see file"))
        fid = owner_call("GET", f"/posts/{pid}")["file_ids"][0]
        r = self.aimost("file", fid, env={"AIMOST_FILES_DIR": str(self.tmp / "files")})
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.strip()
        self.assertTrue(out.endswith(f"{fid}-data_file.txt"), out)
        self.assertEqual(Path(out).read_bytes(), src.read_bytes())
        r = self.aimost("file", fid, str(self.tmp / "x" / "copy.bin"))
        self.assertEqual((self.tmp / "x" / "copy.bin").read_bytes(), src.read_bytes())


@requires_mattermost
class Channel(World):
    def test_show_and_set(self):
        ch = new_channel()
        self.ok("--channel", ch["name"], "--pin", "rules", "Rules: be kind")
        r = self.aimost("channel", "set", ch["name"], "--purpose", "Deploys", "--header", "📌 rules · 📇 agents")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = owner_call("GET", f"/channels/{ch['id']}")
        self.assertEqual((got["purpose"], got["header"]), ("Deploys", "📌 rules · 📇 agents"))
        r = self.aimost("channel", "show", ch["name"])
        self.assertIn("purpose: Deploys", r.stdout)
        self.assertIn("[rules]: Rules: be kind", r.stdout)
        r = self.aimost("channel", "set", ch["name"], "--purpose", "x" * 251)
        self.assertEqual(r.returncode, 2)
        with (paths.config_dir() / "connections" / "it.env").open("a") as f:
            f.write("PRIVATE_GUARD=on\n")
        (paths.config_dir() / "private-patterns").write_text("my-hobby\n")
        r = self.aimost("channel", "set", ch["name"], "--header", "my-hobby links")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual(owner_call("GET", f"/channels/{ch['id']}")["header"], "📌 rules · 📇 agents")


@requires_mattermost
class Who(World):
    def test_chat_side(self):
        r = self.aimost("who", "colleague")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("@colleague", r.stdout)
        r = self.aimost("who", "@devops")
        self.assertIn("[bot]", r.stdout)

    def test_directory_source(self):
        """A directory plugin maps a login of another system to the chat login; the chat checks it exactly."""
        import io
        import contextlib
        cli_who.WHO_SOURCES.append(lambda term: [{"source": "review", "login": "qq-zz", "name": "Col",
                                                  "chat": "colleague"}] if term == "qq-zz" else [])
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli_who.main(["qq-zz", "--via", "it"])
        finally:
            cli_who.WHO_SOURCES.clear()
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("@colleague", out)
        self.assertIn("[login from a directory]", out)
        self.assertIn("review: qq-zz Col → chat @colleague", out)


if __name__ == "__main__":
    unittest.main()
