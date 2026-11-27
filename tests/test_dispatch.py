"""Wake dispatcher — whom to wake and how (delivery: "Waking an idle or closed session", "A resumed session is the same
session", "The wake log records reading, not launching", "Session mode", "Codex uses the same inboxes" — cmd lines).
Oracle: upstream chat-dispatch-wake, chat-dispatch-scope, chat-cli-mode (dispatcher part). A fake ``claude`` logs calls."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import unittest

from fakes import SessionTest

from aimost import dispatch, inbox, paths


class DispatchTest(SessionTest):
    def setUp(self):
        super().setUp()
        self.fake_claude()
        os.environ["AIMOST_WAKE_MIN_S"] = "0"
        os.environ["AIMOST_WAKE_NO_SCOPE"] = "1"
        paths.config_dir().mkdir(parents=True)
        self.wake_list("steward")
        self.sid = self.session("steward", sid="SID-1")
        self.box = inbox.Inbox("steward")
        self.box.put("one", sender="owner1")
        post = self.tmp / "post"
        post.write_text(f'#!/bin/sh\necho "$AIMOST_ROLE $*" >> {self.tmp}/posted\n')
        post.chmod(0o755)
        os.environ["AIMOST_POST_BIN"] = str(post)

    def wake_list(self, *lines):
        paths.wake_list().write_text("".join(x + "\n" for x in lines))

    def once(self, **env):
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            dispatch.pass_once()
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def n(self, needle):
        return self.calls().count(needle)

    def dlog(self):
        return (paths.inbox_dir() / "dispatch.log").read_text()

    def posted(self):
        try:
            return (self.tmp / "posted").read_text()
        except FileNotFoundError:
            return ""


class Wake(DispatchTest):
    def test_activity_gets_the_session_list(self):
        from unittest import mock
        from aimost import activity
        agents = [{"name": "steward", "sessionId": "SID-1", "status": "waiting"}]
        self.set_agents(agents)
        with mock.patch.object(activity, "status_sync") as ss, mock.patch.object(activity, "waiting_notify") as wn:
            self.once()
        ss.assert_called_once_with(agents)
        wn.assert_called_once_with(agents)

    def test_waiting_for_permission_not_woken(self):
        os.utime(self.transcript("SID-1"), (time.time() - 3600,) * 2)
        self.set_agents([{"name": "steward", "sessionId": "SID-1", "status": "waiting"}])
        self.once()
        self.assertEqual(self.calls(), "")

    def test_idle_message_and_verify(self):
        self.set_agents([{"name": "steward", "sessionId": "SID-1", "status": "idle"}])
        self.once()
        self.assertEqual(self.n("-p --model haiku"), 1)
        self.assertIn("named 'steward'", self.calls())
        self.assertIn("aimost dispatch → steward: 1 new in inbox — /inbox", self.calls())
        self.once(AIMOST_WAKE_VERIFY_S="0")
        self.assertIn("wake steward — NOT read within", self.dlog())
        self.box.put("two")
        self.once()
        self.box.unread(ack=True)
        self.once()
        self.assertIn("wake steward — read within", self.dlog())

    def test_only_new_lines_wake_and_min_interval(self):
        self.set_agents([{"name": "steward", "sessionId": "SID-1", "status": "idle"}])
        self.once()
        self.clear_calls()
        self.once()
        self.assertEqual(self.calls(), "", "no new lines since the last wake")
        self.box.put("two")
        self.once(AIMOST_WAKE_MIN_S="120")
        self.assertEqual(self.calls(), "", "not more often than AIMOST_WAKE_MIN_S")
        self.once()
        self.assertEqual(self.n("-p --model"), 1)

    def test_ended_after_message_is_resumed_with_retry_window(self):
        self.set_agents([{"name": "steward", "sessionId": "SID-1", "status": "idle"}])
        self.once()
        self.set_agents([])
        self.clear_calls()
        self.once()
        self.assertEqual(self.n("--resume SID-1"), 1, "closed with unread events after a message wake")
        self.clear_calls()
        self.once()
        self.assertEqual(self.n("--resume"), 0, "a repeated resume — not at once")
        self.once(AIMOST_RESUME_RETRY_S="0")
        self.assertEqual(self.n("--resume"), 1)

    def test_resume_same_name_dir_args_model_settings(self):
        proj = self.tmp / "proj"
        proj.mkdir()
        self.session("side", cwd=str(proj), sid="SID-2")
        self.wake_list("steward --model sonnet", "side")
        inbox.Inbox("side").put("one")
        settings = self.tmp / "settings"
        settings.write_text('#!/bin/sh\n[ "$2" = steward ] && echo \'{"env":{"X":"1"}}\'\nexit 0\n')
        settings.chmod(0o755)
        self.once(AIMOST_WAKE_ARGS="--dangerously-skip-permissions", AIMOST_WAKE_SETTINGS=f"{settings} settings")
        c = self.calls()
        self.assertIn('--bg --dangerously-skip-permissions -n steward --settings {"env":{"X":"1"}} --resume SID-1 '
                      "--model sonnet /inbox @", c)
        self.assertIn(f"--bg --dangerously-skip-permissions -n side --resume SID-2 /inbox @{proj}\n", c,
                      "from the session's own directory; settings silent for this role — no --settings")

    def test_bad_model_not_passed(self):
        self.wake_list("steward --model x;rm")
        self.once()
        self.assertEqual(self.n("--resume SID-1"), 1)
        self.assertNotIn("--model", self.calls())

    def test_finished_background_is_closed_not_busy(self):
        self.set_agents([{"name": "steward", "sessionId": "SID-1", "kind": "background", "state": "blocked"}])
        self.once()
        self.assertEqual((self.n("--resume SID-1"), self.n("-p --model")), (1, 0))

    def test_busy_left_alone_unless_turn_ended_or_silent(self):
        self.set_agents([{"name": "steward", "sessionId": "SID-1", "status": "busy"}])
        self.once()
        self.assertEqual(self.calls(), "", "busy and the transcript fresh")
        self.append("SID-1", {"type": "assistant"}, {"type": "system", "subtype": "turn_duration"},
                    {"type": "queue-operation"})
        self.once()
        self.assertEqual(self.n("-p --model"), 1)
        self.assertIn("busy, but the turn has ended", self.dlog())
        self.box.put("two")
        self.append("SID-1", {"type": "user"})
        os.utime(self.transcript("SID-1"), (time.time() - 300,) * 2)
        self.clear_calls()
        self.once()
        self.assertEqual(self.n("-p --model"), 1)
        self.assertIn("silent for", self.dlog())

    def test_failed_message_retried_next_pass(self):
        exe = self.tmp / "claude" / "claude"
        exe.write_text('#!/bin/sh\nd=$(dirname "$0")\nif [ "$1" = agents ]; then cat "$d/agents.json"; else '
                       'echo "$* @$PWD" >> "$d/calls"; exit 1; fi\n')
        self.set_agents([{"name": "steward", "sessionId": "SID-1", "status": "idle"}])
        self.once()
        self.once()
        self.assertEqual(self.n("-p --model"), 2)
        self.assertIn("claude -p failed, retry on the next pass", self.dlog())

    def test_nobody_to_wake_tells_the_owner_once(self):
        self.wake_list("ghost")
        inbox.set_mode("lonely", "chat")
        inbox.Inbox("lonely").put("hi", via="it")
        self.box.unread(ack=True)
        self.once()
        self.assertRegex(self.posted(), r"^lonely --via it --channel dm ✋ lonely: 1 unread .*not in the wake list.*"
                                        r"`claude -n lonely`")
        self.assertEqual(self.n("--resume"), 0)
        self.once()
        self.assertEqual(self.posted().count("\n"), 1, "the same inbox lines — not repeated")
        inbox.Inbox("ghost").put("hey", via="it")
        self.once(AIMOST_WAKE_HINT="`agent open {role}`")
        self.assertRegex(self.posted(), r"ghost .*no session with this name exists.*`agent open ghost`")

    def test_no_via_no_dm(self):
        self.wake_list()
        inbox.set_mode("steward", "chat")
        self.once()
        self.assertEqual(self.posted(), "")
        self.assertIn("nobody to wake — it is not in the wake list", self.dlog())


class OwnCommand(DispatchTest):
    def test_cmd_line_wakes_with_env_even_live(self):
        out = self.tmp / "cmd.out"
        self.wake_list(f'steward cmd echo "$AIMOST_WAKE_ROLE $AIMOST_WAKE_COUNT $AIMOST_WAKE_PROMPT" > {out}')
        self.set_agents([{"name": "steward", "sessionId": "SID-1", "status": "busy"}])
        self.once()
        self.assertTrue(out.read_text().startswith("steward 1 aimost dispatch → steward: 1 new in the inbox"))
        self.assertIn("own command", self.dlog())
        self.assertEqual(self.calls(), "")

    def test_cmd_failure_retried(self):
        self.wake_list("steward cmd exit 3")
        self.once()
        self.once()
        self.assertEqual(self.dlog().count("own command failed (code 3)"), 2)

    def test_without_claude_only_cmd(self):
        os.environ["AIMOST_CLAUDE_BIN"] = str(self.tmp / "no-such-claude")
        out = self.tmp / "cmd.out"
        self.wake_list(f"steward cmd touch {out}")
        self.once()
        self.assertTrue(out.exists())


class CliMode(DispatchTest):
    def test_cli_never_woken_logged_hourly(self):
        sid = self.session("hand")
        inbox.Inbox("hand").put("task")
        self.set_agents([{"name": "hand", "sessionId": sid, "status": "idle", "kind": "interactive"}])
        self.box.unread(ack=True)
        self.once()
        self.once()
        self.assertEqual(self.n("-p --model"), 0)
        self.assertEqual(self.dlog().count("hand: cli, 1 in the inbox"), 1)

    def test_cli_label_beats_wake_list(self):
        inbox.set_mode("steward", "cli")
        self.once()
        self.assertEqual(self.n("--resume"), 0)
        inbox.set_mode("steward", "chat")
        self.once()
        self.assertEqual(self.n("--resume SID-1"), 1)

    def test_live_background_or_role_without_label_becomes_chat(self):
        self.box.unread(ack=True)
        g = self.session("ghost")
        inbox.Inbox("ghost").put("job")
        self.set_agents([{"name": "ghost", "sessionId": g, "status": "idle", "kind": "background"}])
        self.once()
        self.assertEqual(self.n("named 'ghost'"), 1)
        r = self.session("idler")
        self.append(r, {"type": "agent-setting", "agentSetting": "devops", "sessionId": r})
        inbox.Inbox("idler").put("job")
        self.set_agents([{"name": "idler", "sessionId": r, "status": "idle", "kind": "interactive"}])
        self.once()
        self.assertEqual(self.n("named 'idler'"), 1)
        self.assertEqual(inbox.mode_of("idler")[0], "chat")


class Scope(DispatchTest):
    @unittest.skipUnless(shutil.which("systemd-run") and subprocess.run(
        ["systemctl", "--user", "show-environment"], capture_output=True).returncode == 0, "no user systemd")
    def test_wakes_run_in_their_own_scope(self):
        os.environ.pop("AIMOST_WAKE_NO_SCOPE")
        dispatch._scope_ok = None
        self.addCleanup(setattr, dispatch, "_scope_ok", None)
        exe = self.tmp / "claude" / "claude"
        exe.write_text('#!/bin/sh\nd=$(dirname "$0")\nif [ "$1" = agents ]; then cat "$d/agents.json"; '
                       'else cat /proc/self/cgroup >> "$d/calls"; fi\n')
        self.once()
        self.assertIn("aimost-wake-steward-", self.calls())


class Cli(DispatchTest):
    def test_usage(self):
        self.assertEqual(self.aimost("dispatch").returncode, 2)
        self.assertEqual(self.aimost("dispatch", "once").returncode, 0)


if __name__ == "__main__":
    unittest.main()
