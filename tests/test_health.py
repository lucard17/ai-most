"""``/health`` — the optional built-in plugin (health spec): the targets file, probe rules, contour polls against fake
services on localhost, the call with its rate limit, deadline and delayed answer, and the plugin switch."""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import IsolatedTest

from aimost import commands, plugins
from aimost.commands import Call
from aimost_health import config as hc
from aimost_health import probe, run

HOOK = "/hooks/commands/abc123"


class Fake:
    """A local HTTP server: path → (status, headers, body | callable(handler)); counts hits, records POST bodies."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.hits = []
        self.posts = []
        self.posted = threading.Event()
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.hits.append(self.path)
                r = fake.routes.get(self.path, (404, {}, b""))
                if callable(r):
                    return r(self)
                code, headers, body = r
                body = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fake.posts.append((self.path, json.loads(body)))
                fake.posted.set()
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def dead_url() -> str:
    """An address nobody listens on (connection refused)."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def target(url, path="/health", rule="status", **kw):
    return dict(url=url, path=path, rule=rule, **kw)


class Config(unittest.TestCase):
    def ok(self, cfg):
        hc.validate(cfg)

    def bad(self, cfg, why=None):
        with self.assertRaises(hc.ConfigError) as cm:
            hc.validate(cfg)
        if why:
            self.assertEqual(str(cm.exception), why)

    def test_the_shipped_example_is_valid_once_addresses_are_filled(self):
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "src" / "aimost" / "data" / "config" / "health-targets.example.json"
        cfg = json.loads(p.read_text())
        self.bad(cfg, "bad probe address")          # empty urls: refused until filled in
        for contours in cfg.values():
            for services in contours.values():
                for t in services.values():
                    if t is not None:
                        t["url"] = "http://10.0.0.1:8080"
        self.ok(cfg)

    def test_names(self):
        t = target("http://h:1")
        self.ok({"example": {"staging": {"api": t, "cache": None}}})
        self.bad({"Example": {"staging": {"api": t}}}, "bad name of a domain, contour or service")
        self.bad({"example": {"staging": {"a|b": t}}})
        self.bad({"example": {"s" * 33: {"api": t}}})
        self.bad({"example": {}}, "a domain without contours")
        self.bad({"example": {"staging": {}}}, "a contour without services")
        self.bad([], "an object expected")

    def test_addresses_only_scheme_host_port(self):
        for url in ("http://h:1/x", "http://u:p@h:1", "http://h:1?q", "http://h:1#f", "ftp://h", "http://h:0", "h:1",
                    "http://h:99999", "http://h :1", ""):
            self.bad({"d": {"c": {"s": target(url)}}}, "bad probe address")
        self.ok({"d": {"c": {"s": target("https://h.example:8443/")}}})

    def test_paths(self):
        for path in ("health", "//h/x", "/a/../b", "/a/./b", "/a b", "/a?x"):
            self.bad({"d": {"c": {"s": target("http://h", path=path)}}}, "bad probe")

    def test_rules(self):
        h = "http://h"
        self.bad({"d": {"c": {"s": target(h, rule="tcp")}}}, "bad probe")
        self.bad({"d": {"c": {"s": target(h, extra=1)}}}, "bad probe")
        self.ok({"d": {"c": {"s": target(h, rule="http", redirect={"status": 307, "prefix": "/app/"})}}})
        for r in ({"status": 200, "prefix": "/"}, {"status": True, "prefix": "/"}, {"status": 302}):
            self.bad({"d": {"c": {"s": target(h, rule="http", redirect=r)}}}, "bad redirect rule")
        self.bad({"d": {"c": {"s": target(h, redirect={"status": 302, "prefix": "/"})}}}, "bad redirect rule")
        mode = {"field": "runMode", "expected": "live", "allowed": ["live", "off"]}
        self.ok({"d": {"c": {"s": target(h, rule="role", mode=mode)}}})
        for m in (dict(mode, expected="x"), dict(mode, off_status=1), dict(mode, field="a.b"), {"field": "x"}):
            self.bad({"d": {"c": {"s": target(h, mode=m)}}}, "bad mode rule")
        self.bad({"d": {"c": {"s": target(h, rule="http", mode=mode)}}}, "bad mode rule")

    def test_shown_texts_cannot_carry_mentions_links_or_table_breaks(self):
        h = "http://h"
        self.ok({"d": {"c": {"s": target(h, reasons={"worker is starting": "starting up"})}}})
        for shown in ("@channel down", "see http://x", "a | b", "a\nb", "`x`", "x" * 81, "", "_x"):
            self.bad({"d": {"c": {"s": target(h, reasons={"r": shown})}}}, "bad reasons")
            self.bad({"d": {"c": {"s": target(h, details=[{"field": "x", "label": shown, "count": True}])}}},
                     "bad details")
        self.ok({"d": {"c": {"s": target(h, details=[{"field": "queue.depth", "label": "queue", "count": True},
                                                     {"field": "src", "label": "source", "allowed": ["a", "b"]}])}}})
        for d in ({"field": "x", "label": "x", "count": False}, {"field": "x", "label": "x", "allowed": ["A"]},
                  {"field": "x", "label": "x"}, {"field": "1x", "label": "x", "count": True}):
            self.bad({"d": {"c": {"s": target(h, details=[d])}}}, "bad details")


class Targets(IsolatedTest):
    def test_reread_on_change_bad_version_keeps_the_last_good(self):
        logs = []
        t = hc.Targets(logs.append)
        path = self.tmp / "targets.json"
        self.assertEqual(t.load(str(path)), {}, "no file — nothing configured")
        good = {"d": {"c": {"s": target("http://h:1")}}}
        path.write_text(json.dumps(good))
        self.assertEqual(t.load(str(path)), good)
        tmp = self.tmp / "new.json"
        tmp.write_text(json.dumps({"d": {"c": {"s": target("http://h:1/secret-path")}}}))
        os.replace(tmp, path)
        self.assertEqual(t.load(str(path)), good)
        self.assertEqual(logs, ["health: targets refused (bad probe address), the previous ones stay"])
        tmp.write_text("{not json")
        os.replace(tmp, path)
        self.assertEqual(t.load(str(path)), good)
        self.assertIn("(JSONDecodeError)", logs[-1])
        self.assertNotIn("not json", "".join(logs))
        good2 = {"d": {"c": {"s": None}}}
        tmp.write_text(json.dumps(good2))
        os.replace(tmp, path)
        self.assertEqual(t.load(str(path)), good2)
        self.assertEqual(t.load(None), {})


class Parse(unittest.TestCase):
    def p(self, service, tgt, code, body=b"", location=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return probe.parse(service, tgt, code, location, raw, 5)

    def test_http_rule_and_redirect_checked_without_following(self):
        t = {"rule": "http", "redirect": {"status": 307, "prefix": "/app/"}}
        self.assertEqual(self.p("front", t, 204).status, "ok")
        self.assertEqual(self.p("front", t, 307, location="/app/login").status, "ok")
        self.assertEqual(self.p("front", t, 307, location="http://other/app/x").status, "ok", "path is what counts")
        self.assertEqual(self.p("front", t, 307, location="/elsewhere").reason, "unexpected redirect")
        self.assertEqual(self.p("front", t, 302, location="/app/").reason, "unexpected redirect")
        self.assertEqual(self.p("front", {"rule": "http"}, 301, location="/app/").reason, "unexpected redirect")
        self.assertEqual(self.p("front", t, 502).reason, "HTTP 502")

    def test_status_rule(self):
        t = {"rule": "status"}
        self.assertEqual(self.p("db", t, 200, {"status": "ok"}).status, "ok")
        self.assertEqual(self.p("db", t, 200, {"status": "OK"}).reason, probe.FAILED)
        self.assertEqual(self.p("db", t, 200, b"<html>").reason, "invalid JSON")
        self.assertEqual(self.p("db", t, 200, [1]).reason, "invalid JSON")
        self.assertEqual(self.p("db", t, 500, b"oops").reason, "HTTP 500")
        self.assertEqual(self.p("db", t, 500, {"status": "ok"}).reason, "HTTP 500")

    def test_role_rule_and_reasons(self):
        t = {"rule": "role", "reasons": {"worker is starting": "starting up"}}
        self.assertEqual(self.p("api", t, 200, {"ok": True, "role": "api"}).status, "ok")
        self.assertEqual(self.p("api", t, 200, {"ok": True, "role": "other"}).reason, "the answer is not from this service")
        self.assertEqual(self.p("api", t, 200, {"ok": 1, "role": "api"}).reason, probe.FAILED)
        r = self.p("api", t, 503, {"ok": False, "role": "api", "reason": "worker is starting"})
        self.assertEqual((r.status, r.reason), ("fail", "starting up"), "a known reason, even with 503")
        r = self.p("api", t, 503, {"ok": False, "role": "api", "reason": "@channel see http://evil"})
        self.assertEqual(r.reason, probe.FAILED, "an unknown reason is never shown")

    def test_mode(self):
        mode = {"field": "runMode", "expected": "live", "allowed": ["live", "off"]}
        t = {"rule": "role", "mode": mode}
        body = {"ok": True, "role": "api"}
        self.assertEqual(self.p("api", t, 200, dict(body, runMode="live")).status, "ok")
        self.assertEqual(self.p("api", t, 200, dict(body, runMode="off")).reason, "differs from the contour's setting")
        self.assertEqual(self.p("api", t, 200, dict(body, runMode="wild")).reason, "unknown mode")
        self.assertEqual(self.p("api", t, 200, body).reason, "unknown mode")
        off = {"rule": "role", "mode": dict(mode, expected="off", off_status=True)}
        r = self.p("api", off, 200, dict(body, runMode="off"))
        self.assertEqual((r.status, r.reason), ("off", "off by the contour's setting"))
        self.assertEqual(self.p("api", dict(off, mode=dict(mode, expected="off")), 200, dict(body, runMode="off")).status,
                         "ok", "without off_status a matching off is green")

    def test_details_only_through_the_whitelist(self):
        t = {"rule": "status", "details": [{"field": "jobs", "label": "jobs", "count": True},
                                           {"field": "engine.state", "label": "engine", "allowed": ["active", "off"]}]}
        logs = []
        r = probe.parse("db", t, 200, None, json.dumps({"status": "ok", "jobs": 16, "engine": {"state": "active"},
                                                       "secret": "x"}).encode(), 1, logs.append)
        self.assertEqual(r.details, (("jobs", 16), ("engine", "active")))
        r = probe.parse("db", t, 200, None, json.dumps({"status": "ok", "jobs": True,
                                                       "engine": {"state": "@here"}}).encode(), 1, logs.append)
        self.assertEqual(r.details, (), "a bool is not a count; a value outside the list is dropped")
        self.assertEqual(len(logs), 2)
        self.assertNotIn("@here", "".join(logs))
        r = self.p("db", t, 200, {"status": "ok", "jobs": -1})
        self.assertEqual(r.details, ())


class Poll(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.addCleanup(self.fake.close)

    def test_parallel_probes_noprobe_and_one_failure(self):
        self.fake.routes.update({"/a": (200, {}, {"status": "ok"}), "/b": (500, {}, b"")})
        t0 = time.monotonic()
        rows = probe.poll({"a": target(self.fake.url, "/a"), "b": target(self.fake.url, "/b"),
                           "c": target(dead_url(), "/c"), "cache": None})
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual({k: (r.status, r.reason) for k, r in rows.items()},
                         {"a": ("ok", ""), "b": ("fail", "HTTP 500"), "c": ("fail", "connection error"),
                          "cache": ("noprobe", "")})

    def test_unreachable_contour_is_one_finding_not_every_service_down(self):
        rows = probe.poll({"a": target(dead_url()), "b": target(dead_url()), "cache": None})
        self.assertEqual({k: r.status for k, r in rows.items()}, {"a": "unreachable", "b": "unreachable", "cache": "noprobe"})
        self.assertEqual(rows["a"].reason, probe.UNREACHABLE)

    def test_the_deadline_covers_a_body_sent_byte_by_byte(self):
        def drip(h):
            h.send_response(200)
            h.send_header("Content-Length", "100")
            h.end_headers()
            for _ in range(100):
                try:
                    h.wfile.write(b" ")
                    h.wfile.flush()
                except OSError:
                    return
                time.sleep(0.1)
        self.fake.routes["/slow"] = drip
        t0 = time.monotonic()
        rows = probe.poll({"s": target(self.fake.url, "/slow")}, probe_s=0.5)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual((rows["s"].status, rows["s"].reason), ("fail", "deadline"))
        self.assertTrue(rows["s"].replied, "it answered: not a lost link")

    def test_large_bodies(self):
        big = b"x" * (probe.MAX_BODY + 1)

        def no_length(h):
            h.send_response(200)
            h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(big)
        self.fake.routes.update({"/big": (200, {}, big), "/exact": (200, {}, b"x" * probe.MAX_BODY), "/nolen": no_length})
        rows = probe.poll({n: target(self.fake.url, "/" + n) for n in ("big", "exact", "nolen")})
        self.assertEqual(rows["big"].reason, "answer too large")
        self.assertEqual(rows["nolen"].reason, "answer too large")
        self.assertEqual(rows["exact"].reason, "invalid JSON", "exactly the limit is read")


class FakeEdge:
    def __init__(self, mm_url, **env):
        self.mm_url = mm_url
        self.env = dict(env)
        self.logs = []
        self.after = []

    def log(self, m):
        self.logs.append(m)

    def audits(self, event=None):
        out = [json.loads(m.split(" ", 2)[2]) for m in self.logs if m.startswith("health audit ")]
        return [a for a in out if event is None or a["event"] == event]


class Calls(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.svc = Fake({"/ok": (200, {}, {"status": "ok"}), "/bad": (503, {}, b"")})
        self.mm = Fake()
        self.addCleanup(self.svc.close)
        self.addCleanup(self.mm.close)
        self.path = self.tmp / "targets.json"
        self.write({"example": {"staging": {"api": target(self.svc.url, "/ok"), "worker": target(self.svc.url, "/bad"),
                                            "cache": None},
                                "prod": {"api": target(self.svc.url, "/ok"), "cache": None}}})
        self.edge = FakeEdge(self.mm.url, EDGE_HEALTH_TARGETS=str(self.path))
        run._STATE.clear()

    def write(self, cfg):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg))
        os.replace(tmp, self.path)

    def call(self, text="", roles="system_user", response_url="http://chat.example" + HOOK, uid="u1"):
        c = Call(edge=self.edge, name="health", sub="", args=text.split(), user_id=uid, user_name="ann",
                 caller={"id": uid, "roles": roles}, form={"response_url": response_url})
        return run.handler(c)

    def answer(self, text="", **kw):
        """The immediate answer, then the delayed one (sent after the edge ran its after-reply work)."""
        self.mm.posted.clear()
        n = len(self.mm.posts)
        first = self.call(text, **kw)
        todo, self.edge.after = self.edge.after, []
        for f in todo:
            f()
        if not todo:
            return first, None
        self.assertTrue(self.mm.posted.wait(8), "no delayed answer")
        deadline = time.monotonic() + 2
        while len(self.mm.posts) <= n and time.monotonic() < deadline:
            time.sleep(0.01)
        path, body = self.mm.posts[n]
        self.assertEqual(path, HOOK, "only the hook path of response_url, to the edge's own Mattermost")
        self.assertEqual(body["response_type"], "ephemeral")
        return first, body["text"]

    def test_summary_domain_and_service_views(self):
        first, text = self.answer()
        self.assertEqual(first, run.CHECKING)
        self.assertEqual(text.split("\n")[0], "🔴 EXAMPLE: 2/3 ok · 🔴 1 failed · ⚪ 2 no probe")
        self.assertIn("| staging | worker | 🔴 HTTP 503 |", text)
        self.assertNotIn("| prod |", text, "the summary shows only rows that are not green")
        self.assertIn("Details — `/health EXAMPLE`", text)
        self.assertNotIn("127.0.0.1", text)
        _, text = self.answer("EXAMPLE")
        self.assertIn("| service | staging | prod |", text)
        self.assertIn("| api | 🟢 | 🟢 |", text)
        self.assertIn("| worker | 🔴 | — |", text, "a contour without that service: —")
        self.assertIn("| cache | ⚪ | ⚪ |", text)
        self.assertIn("- worker staging: HTTP 503", text)
        self.assertIn("staging: checked", text, "inside the window: the last result, marked")
        _, text = self.answer("example worker")
        self.assertIn("| contour | worker |", text)
        self.assertIn("| prod | — |", text)

    def test_rate_limit_one_poll_per_contour_per_window(self):
        self.answer()
        hits = len(self.svc.hits)
        self.assertEqual(hits, 3)
        _, text = self.answer()
        self.assertEqual(len(self.svc.hits), hits, "a second call inside the window does not probe")
        self.assertRegex(text, r"staging: checked \d+ s ago")
        polls = self.edge.audits("poll")
        self.assertEqual(len(polls), 2, "one poll per contour")
        self.assertEqual({(p["contour"], p["answered"], p["connection_errors"]) for p in polls},
                         {("staging", 2, 0), ("prod", 1, 0)})

    def test_window_passes(self):
        self.edge.env["EDGE_HEALTH_MIN_GAP_S"] = "0.2"
        self.answer()
        time.sleep(0.3)
        _, text = self.answer()
        self.assertEqual(len(self.svc.hits), 6)
        self.assertNotIn("checked", text)

    def test_a_call_joins_a_running_poll(self):
        def slow(h):
            time.sleep(0.4)
            body = b'{"status": "ok"}'
            h.send_response(200)
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
        self.svc.routes["/ok"] = slow
        self.write({"example": {"staging": {"api": target(self.svc.url, "/ok")}}})
        self.call()
        a = self.edge.after
        self.edge.after = []
        self.call()
        b = self.edge.after
        self.edge.after = []
        for f in a + b:
            f()
        deadline = time.monotonic() + 5
        while len(self.mm.posts) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.svc.hits), 1, "the second call waited for the same poll")
        self.assertEqual([p[1]["text"].split("\n")[0] for p in self.mm.posts], ["🟢 EXAMPLE: 1/1 ok"] * 2)

    def test_deadline_answers_and_drops_the_late_result(self):
        release = threading.Event()

        def hang(h):
            release.wait(5)
        self.svc.routes["/ok"] = hang
        self.addCleanup(release.set)
        self.write({"example": {"staging": {"api": target(self.svc.url, "/ok")}}})
        self.edge.env.update(EDGE_HEALTH_DEADLINE_S="0.4", EDGE_HEALTH_PROBE_S="1.5")
        run._STATE.clear()
        t0 = time.monotonic()
        _, text = self.answer()
        self.assertLess(time.monotonic() - t0, 1.2)
        self.assertEqual(text, run.TIMEOUT)
        # a poll error is not a result: no new poll before RETRY_S, the call says when
        _, text = self.answer()
        self.assertEqual(text.split("\n")[0], "❗ poll error, retry in 2 s")
        self.assertEqual(len(self.svc.hits), 1)
        time.sleep(1.4)
        self.assertEqual(len(self.mm.posts), 2, "the late result is dropped")
        self.assertEqual([a["outcome"] for a in self.edge.audits("call")], ["timeout", "completed"])
        self.assertEqual([(a["error_class"], a["answered"]) for a in self.edge.audits("poll")], [("TimeoutError", None)])

    def test_unreachable_contour(self):
        self.write({"example": {"staging": {"api": target(self.svc.url, "/ok")},
                                "prod": {"api": target(dead_url()), "db": target(dead_url())}}})
        _, text = self.answer()
        self.assertEqual(text.split("\n")[0], "❔ EXAMPLE: 1/1 ok · prod — no link to contour")
        self.assertIn("| prod | api | ❔ no link to contour |", text)

    def test_refusals_and_help(self):
        self.assertEqual(self.call(roles="system_guest"), run.MEMBERS)
        self.assertEqual(self.call(roles=""), run.MEMBERS)
        self.assertEqual(self.call(response_url="http://chat.example/api/v4/posts"), run.BAD_URL)
        self.assertEqual(self.call(response_url="http://chat.example" + HOOK + "/../x"), run.BAD_URL)
        self.assertEqual(self.call(response_url=""), run.BAD_URL)
        self.assertIn("No domain `FOO`. Domains: EXAMPLE.", self.call("foo"))
        self.assertIn("Unknown service. Services: api, worker, cache.", self.call("example nope"))
        self.assertIn("Domains: EXAMPLE", self.call("example api extra"))
        self.assertEqual(self.edge.after, [], "nothing polled for a refused call")
        self.edge.env["EDGE_HEALTH"] = "off"
        self.assertEqual(self.call(), run.OFF)
        self.edge.env["EDGE_HEALTH"] = ""
        self.path.unlink()
        self.assertEqual(self.call(), "No domains are configured for checks.")
        self.assertEqual(self.svc.hits, [])
        outcomes = [a["outcome"] for a in self.edge.audits("call")]
        self.assertEqual(outcomes, ["denied", "denied", "invalid_response_url", "invalid_response_url",
                                    "invalid_response_url", "invalid_arguments", "invalid_arguments",
                                    "invalid_arguments", "disabled", "unconfigured"])

    def test_audit_has_no_addresses_or_reasons(self):
        self.answer("example")
        log = "\n".join(self.edge.logs)
        self.assertNotIn("127.0.0.1", log)
        self.assertNotIn("HTTP 503", log)
        call = self.edge.audits("call")[-1]
        self.assertEqual((call["user_id"], call["args"], call["outcome"]), ("u1", ["example"], "completed"))
        self.assertEqual(call["domains"]["example"], {"ok": 2, "fail": 1, "noprobe": 2})

    def test_a_changed_contour_forgets_its_last_poll(self):
        self.answer()
        self.write({"example": {"staging": {"api": target(self.svc.url, "/ok")},
                                "prod": {"api": target(self.svc.url, "/ok"), "cache": None}}})
        _, text = self.answer()
        self.assertIn("prod: checked", text)
        self.assertNotIn("staging: checked", text)


class ClaimAfterDeadline(unittest.TestCase):
    def test_no_answer_is_claimed_once_the_deadline_passed(self):
        h = run.Health(lambda m: None)
        c = run.Call(h, None, HOOK, "u1", [])
        c.expires = time.monotonic() - 0.01
        self.assertFalse(c.claim("completed", {}))
        c.expires = time.monotonic() + 5
        self.assertTrue(c.claim("completed", {}))
        self.assertFalse(c.claim("completed", {}), "once")


class ThroughTheEdge(IsolatedTest):
    """The real Edge: key check, the caller from Mattermost, the form reaching the handler, work after the answer."""

    def setUp(self):
        super().setUp()
        from aimost import edge as edge_mod
        self.svc = Fake({"/ok": (200, {}, {"status": "ok"})})
        self.mm = Fake()
        self.addCleanup(self.svc.close)
        self.addCleanup(self.mm.close)
        path = self.tmp / "t.json"
        path.write_text(json.dumps({"example": {"staging": {"api": target(self.svc.url, "/ok")}}}))
        users = {"/users/u1": {"id": "u1", "username": "ann", "roles": "system_user"}}
        self.edge = edge_mod.Edge(env={"EDGE_MM_URL": self.mm.url, "EDGE_SLASH_SECRET": "s3", "EDGE_SECRET": "x",
                                       "EDGE_HEALTH_TARGETS": str(path)},
                                  call=lambda m, p, b=None: users.get(p) or [], log=lambda m: None)
        self._reg_saved = {k: dict(v) for k, v in commands._REG.items()}
        self.addCleanup(lambda: (commands._REG.clear(), commands._REG.update(self._reg_saved)))
        commands._REG.pop("health", None)
        import aimost_health
        aimost_health.register(type("R", (), {"command": staticmethod(commands.register)})())
        run._STATE.clear()

    def post(self, send, k=None):
        import urllib.parse as up
        body = up.urlencode({"user_id": "u1", "text": "example", "response_url": "http://x" + HOOK}).encode()
        self.edge.serve_post(f"/health?k={k or commands.slash_key('s3', 'health')}", body, send)

    def test_immediate_answer_first_then_the_delayed_one(self):
        order = []
        self.post(lambda code, obj: order.append((code, obj["text"])))
        self.assertEqual(order, [(200, run.CHECKING)])
        self.assertTrue(self.mm.posted.wait(5))
        self.assertTrue(self.mm.posts[0][1]["text"].startswith("🟢 EXAMPLE: 1/1 ok"))
        self.assertEqual(self.edge.after, [])

    def test_no_delayed_answer_when_the_immediate_one_was_not_sent(self):
        def broken(code, obj):
            raise BrokenPipeError()
        with self.assertRaises(OSError):
            self.post(broken)
        self.assertEqual(self.edge.after, [])
        self.assertFalse(self.mm.posted.wait(1.5))
        self.assertEqual(self.svc.hits, [], "nothing polled")

    def test_wrong_key(self):
        got = []
        self.post(lambda code, obj: got.append(code), k="0" * 32)
        self.assertEqual(got, [403])
        self.assertEqual(self.edge.after, [])


class Switch(IsolatedTest):
    def setUp(self):
        super().setUp()
        self._saved_reg = {k: dict(v) for k, v in commands._REG.items()}
        self._saved_pl = (dict(plugins._plugins), plugins._loaded)
        self.addCleanup(self.restore)

    def restore(self):
        commands._REG.clear()
        commands._REG.update(self._saved_reg)
        plugins._plugins.clear()
        plugins._plugins.update(self._saved_pl[0])
        plugins._loaded = self._saved_pl[1]

    def test_off_unless_listed(self):
        r = self.aimost("commands", "list")
        self.assertNotIn("/health", r.stdout)
        r = self.aimost("plugins")
        self.assertIn("health", r.stdout)
        self.assertRegex(r.stdout, r"health .*built-in \(optional\)")
        plugins._plugins["health"] = plugins.Plugin("health", "built-in (optional)", True, optional=True)
        plugins._plugins["gitlab"] = plugins.Plugin("gitlab", "built-in", True)
        self.assertFalse(plugins.enabled("health", None))
        self.assertFalse(plugins.enabled("health", {"PLUGINS": "other"}))
        self.assertTrue(plugins.enabled("health", {"PLUGINS": "health"}))
        self.assertTrue(plugins.enabled("gitlab", None), "a default built-in stays on")
        os.environ["AIMOST_PLUGINS"] = "health"
        self.assertTrue(plugins.enabled("health", None))
        del os.environ["AIMOST_PLUGINS"]
        r = self.aimost("commands", "list", env={"AIMOST_PLUGINS": "health"})
        self.assertIn("/health [domain [service]]", r.stdout)
        cdir = self.tmp / "config" / "connections"
        cdir.mkdir(parents=True)
        (cdir / "a.env").write_text("URL=http://x\nBOT_TOKEN=t\n")
        self.assertNotIn("/health", self.aimost("commands", "list").stdout)
        (cdir / "a.env").write_text("URL=http://x\nBOT_TOKEN=t\nPLUGINS=health\n")
        self.assertIn("/health", self.aimost("commands", "list").stdout)


if __name__ == "__main__":
    unittest.main()
