"""One probe and one contour poll (health spec): HTTP GET on the configured address, parsed by the probe's rule.

A probe has one deadline (``probe_s``, 1 s) from connecting to the last byte, not a per-read socket timeout: a service that
sends its body a byte at a time is cut off when the deadline passes. The body is read up to ``MAX_BODY``; more is a
failure. Redirects are never followed. A row says what the chat may show: status, time, a reason made by the edge from
known words, details passed through the whitelist of the targets file — no text from the body otherwise.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.parse
from typing import Callable, Dict, NamedTuple, Optional

from .config import origin

MAX_BODY = 64 * 1024
FAILED = "probe returned an error"
UNREACHABLE = "no link to contour"


class Row(NamedTuple):
    status: str               # ok | off | fail | noprobe | unreachable
    ms: int = 0
    reason: str = ""
    replied: bool = False     # the service got as far as an HTTP status line
    details: tuple = ()       # ((label, value), …)


def _get(body, field: str):
    value = body
    for key in field.split("."):
        if not isinstance(value, dict) or key not in value:
            return None, False
        value = value[key]
    return value, True


def details(target: dict, body, log: Callable[[str], None] = lambda m: None) -> tuple:
    out = []
    for d in target.get("details", ()):
        value, found = _get(body, d["field"])
        if not found:
            continue
        ok = (type(value) is int and value >= 0) if d.get("count") else (isinstance(value, str) and value in d["allowed"])
        if ok:
            out.append((d["label"], value))
        else:
            log(f"health: a value of {d['field']} outside its list — not shown")
    return tuple(out)


def parse(service: str, target: dict, code: int, location: Optional[str], raw: bytes, ms: int,
          log: Callable[[str], None] = lambda m: None) -> Row:
    def row(status, reason="", det=()):
        return Row(status, ms, reason, True, det)

    rule = target["rule"]
    if rule == "http":
        if 200 <= code < 300:
            return row("ok")
        if 300 <= code < 400:
            want = target.get("redirect")
            try:
                path = urllib.parse.urlsplit(location or "").path
            except ValueError:
                path = ""
            if want and code == want["status"] and path.startswith(want["prefix"]):
                return row("ok")
            return row("fail", "unexpected redirect")
        return row("fail", f"HTTP {code}")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        return row("fail", "invalid JSON" if code == 200 else f"HTTP {code}")
    if rule == "role" and body.get("role") != service:
        return row("fail", "the answer is not from this service")
    det = details(target, body, log)

    def failed():
        why = body.get("reason")
        return row("fail", target.get("reasons", {}).get(why, FAILED) if isinstance(why, str) else FAILED, det)

    if rule == "role" and body.get("ok") is False:
        return failed()                       # a service saying why it is not ready — read even with 503
    if code != 200:
        return row("fail", f"HTTP {code}")
    if (body.get("ok") is False or (rule == "role" and body.get("ok") is not True)
            or (rule == "status" and body.get("status") != "ok")):
        return failed()
    mode = target.get("mode")
    if mode:
        actual = body.get(mode["field"])
        if actual not in mode["allowed"]:
            return row("fail", "unknown mode", det)
        if actual != mode["expected"]:
            return row("fail", "differs from the contour's setting", det)
        if actual == "off" and mode.get("off_status"):
            return row("off", "off by the contour's setting", det)
    return row("ok", det=det)


def connection(address: urllib.parse.SplitResult, timeout: float) -> http.client.HTTPConnection:
    cls = http.client.HTTPSConnection if address.scheme == "https" else http.client.HTTPConnection
    return cls(address.hostname, port=address.port, timeout=timeout)


def shutdown(sock) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _Response(http.client.HTTPResponse):
    def __init__(self, probe: "Probe", *a, **kw):
        self.probe = probe
        super().__init__(*a, **kw)

    def _read_status(self):
        status = super()._read_status()
        self.probe.replied = True
        return status


class Probe:
    def __init__(self, service: str, target: dict, probe_s: float, log: Callable[[str], None]):
        self.service, self.target, self.log = service, target, log
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.sock = None
        self.replied = False
        self.error: Optional[BaseException] = None
        self.row: Optional[Row] = None
        self.started = time.monotonic()
        self.deadline = self.started + probe_s
        self.finished = None
        self.probe_s = probe_s

    def elapsed(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def failure(self, reason: str) -> Row:
        return Row("fail", self.elapsed(), reason, self.replied)

    def read(self) -> Row:
        conn = connection(origin(self.target["url"]), self.probe_s)
        try:
            conn.response_class = lambda *a, **kw: _Response(self, *a, **kw)
            conn.connect()
            with self.lock:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                self.sock = conn.sock
                self.sock.settimeout(remaining)
            conn.request("GET", self.target["path"], headers={"Connection": "close"})
            with conn.getresponse() as r:
                if r.length is not None and r.length > MAX_BODY:
                    return self.failure("answer too large")
                raw = r.read(MAX_BODY + 1)        # one byte over tells exactly 64 KiB from a larger body without a length
                if len(raw) > MAX_BODY:
                    return self.failure("answer too large")
                if r.length is not None and r.length != 0:
                    return self.failure("incomplete HTTP answer")
                return parse(self.service, self.target, r.status, r.getheader("Location"), raw, self.elapsed(), self.log)
        finally:
            conn.close()

    def run(self) -> None:
        try:
            self.row = self.read()
        except (TimeoutError, socket.timeout):
            self.row = self.failure("deadline")
        except OSError:                       # before HTTPException: RemoteDisconnected is both
            self.row = self.failure("connection error")
        except http.client.HTTPException:
            self.replied = True
            self.row = self.failure("invalid HTTP answer")
        except Exception as x:
            self.error = x
        finally:
            self.finished = time.monotonic()
            self.ready.set()

    def result(self) -> Row:
        if self.ready.wait(max(0.0, self.deadline - time.monotonic())) and self.finished <= self.deadline:
            if self.error is not None:
                raise self.error
            return self.row
        with self.lock:
            if self.sock is not None:
                shutdown(self.sock)
        return self.failure("deadline")


def poll(services: Dict[str, Optional[dict]], probe_s: float = 1.0,
         log: Callable[[str], None] = lambda m: None) -> Dict[str, Row]:
    """All probes of one contour in parallel. When none of them got an HTTP answer, the contour is unreachable — its rows
    say so, instead of every service looking down."""
    probes = {}
    for name, target in services.items():
        if target is not None:
            p = probes[name] = Probe(name, target, probe_s, log)
            threading.Thread(target=p.run, daemon=True).start()
    rows = {name: probes[name].result() if name in probes else Row("noprobe") for name in services}
    if probes and not any(rows[n].replied for n in probes):
        for n in probes:
            rows[n] = rows[n]._replace(status="unreachable", reason=UNREACHABLE)
    return rows
