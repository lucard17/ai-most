"""``/health`` in the edge (health spec): the call, the rate limit per contour, the delayed answer, the report.

The edge serves one request at a time, so the handler does not wait for the network: it answers "⏳ checking…" at once and
leaves the polls to threads started after that answer is sent (``edge.after``). The result goes back as an ephemeral delayed
answer to the call's ``response_url`` — only its ``/hooks/commands/<id>`` path is taken, the host is the edge's own
Mattermost, so a forged form cannot send the edge elsewhere.

Rate limit: a contour is polled at most once per ``min_gap`` (10 s). A call while a poll runs joins it; a call inside the
window after a finished poll takes its result, marked "checked N s ago". "No link to contour" is a result too. Only a poll
error (an exception, the call deadline) is not: the next poll may start ``RETRY_S`` after it.

Deadline: the whole call has ``deadline`` (5 s). A watcher timer — not the poll thread, whose ``finally`` would not run
if it hung — sends "❗ poll error: deadline" when nothing was sent by then, and a late result is dropped.

Audit: one ``health audit {json}`` log line per call (who, arguments, outcome, counts per domain) and per contour poll
(answered, connection errors, duration) — no addresses, reasons or rows.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import threading
import time
import urllib.parse
from collections import Counter
from typing import Callable, Dict, List, Optional, Tuple

from . import probe
from .config import Targets, origin

HOOK = re.compile(r"/hooks/commands/[A-Za-z0-9]{1,128}")
RETRY_S = 2
POST_ATTEMPT_S = 1.0
ICONS = {"ok": "🟢", "off": "🟡", "fail": "🔴", "noprobe": "⚪", "unreachable": "❔"}
TIMEOUT = "❗ poll error: deadline"
CHECKING = "⏳ checking…"
OFF = "The /health command is turned off."
MEMBERS = "Open to members of the chat."
BAD_URL = "Could not accept the address for the delayed answer."


def audit(log: Callable[[str], None], event: str, **fields) -> None:
    rec = {"event": event, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
    log("health audit " + json.dumps(rec, separators=(",", ":"), ensure_ascii=False))


class Poll:
    """One poll of one contour; ``finish`` is called under ``Health.lock`` and returns the audit record (or None)."""

    def __init__(self, domain: str, contour: str, deadline: float):
        self.domain, self.contour, self.deadline = domain, contour, deadline
        self.ready = threading.Event()
        self.started: Optional[float] = None
        self.end: Optional[float] = None
        self.rows: Optional[Dict[str, probe.Row]] = None
        self.error: Optional[str] = None

    def finish(self, now: float, *, rows=None, error: Optional[str] = None, error_class: str = "") -> Optional[dict]:
        if self.end is not None:
            return None
        if now >= self.deadline or error == TIMEOUT:
            rows, error, error_class = None, TIMEOUT, "TimeoutError"
        self.end, self.rows, self.error = now, rows, error
        self.ready.set()
        if self.started is None:
            return None
        rec = {"domain": self.domain, "contour": self.contour, "duration_ms": int((now - self.started) * 1000),
               "answered": None, "connection_errors": None}
        if rows is not None:
            rec["answered"] = sum(r.replied for r in rows.values())
            rec["connection_errors"] = sum(not r.replied and r.status != "noprobe" for r in rows.values())
        if error_class:
            rec["error_class"] = error_class
        return rec


class Health:
    def __init__(self, log: Callable[[str], None], min_gap: float = 10, deadline: float = 5, probe_s: float = 1):
        self.log = log
        self.min_gap, self.deadline, self.probe_s = min_gap, deadline, probe_s
        self.lock = threading.Lock()
        self.polls: Dict[Tuple[str, str], Poll] = {}
        self.targets = Targets(log)

    def config(self, path: Optional[str]) -> Dict[str, dict]:
        """Read in the request thread only; poll threads get a snapshot. A changed contour forgets its last poll."""
        old = self.targets.config
        new = self.targets.load(path)
        if new is not old:
            with self.lock:
                for key in list(self.polls):
                    d, c = key
                    if new.get(d, {}).get(c) != old.get(d, {}).get(c):
                        del self.polls[key]
        return new

    def reserve(self, domains: Dict[str, dict], received: float, deadline: float, now: float):
        """Under ``lock``: per contour — a new poll, the running one, or the last result. -> (selected, start, expired)."""
        selected, start, expired = {}, [], []
        for domain, contours in domains.items():
            for contour, services in contours.items():
                key = domain, contour
                p = self.polls.get(key)
                age = pause = None
                if p is not None and p.end is None and now >= p.deadline:
                    expired.append(p.finish(now, error=TIMEOUT))
                if p is None or (p.end is not None and now - p.end >= (RETRY_S if p.error else self.min_gap)):
                    p = self.polls[key] = Poll(domain, contour, deadline)
                    start.append((p, services))
                elif p.end is not None and p.end <= received:
                    if p.error:
                        pause = max(1, math.ceil(RETRY_S - (now - p.end)))
                    else:
                        age = int(now - p.end)
                selected[key] = p, age, pause
        return selected, start, expired

    def run_poll(self, p: Poll, services: Dict[str, Optional[dict]]) -> None:
        with self.lock:
            now = time.monotonic()
            if p.end is not None or now >= p.deadline:
                p.finish(now, error=TIMEOUT)
                return
            p.started = now
        try:
            rows = probe.poll(services, self.probe_s, self.log)
        except Exception as x:
            with self.lock:
                rec = p.finish(time.monotonic(), error=f"❗ poll error: {type(x).__name__}", error_class=type(x).__name__)
        else:
            with self.lock:
                rec = p.finish(time.monotonic(), rows=rows)
        if rec:
            audit(self.log, "poll", **rec)


# ── the report
def table(columns: List[str], rows: List[List[str]]) -> str:
    def line(cells):
        return "| " + " | ".join(cells) + " |"
    return "\n".join([line(columns), line(["---"] * len(columns)), *(line(r) for r in rows)])


def summary(label: str, entries, counts: Counter) -> str:
    total = counts["ok"] + counts["fail"] + counts["off"]
    missing = list(dict.fromkeys(c for c, _, r in entries if r.status == "unreachable"))
    icon = ("🔴" if counts["fail"] or counts["poll_error"] else "❔" if missing else "🟡" if counts["off"]
            else "🟢" if total else "⚪")
    parts = [f"{icon} {label}: {counts['ok']}/{total} ok"]
    for status, caption in (("fail", "failed"), ("off", "off"), ("noprobe", "no probe")):
        if counts[status]:
            parts.append(f"{ICONS[status]} {counts[status]} {caption}")
    if missing:
        parts.append(f"{', '.join(missing)} — {probe.UNREACHABLE}")
    return " · ".join(parts)


def report(selected, domain_view: Optional[str], service_view: Optional[str]) -> Tuple[str, dict]:
    answers: Dict[str, dict] = {}
    ages: Dict[str, dict] = {}
    for (domain, contour), (p, age, pause) in selected.items():
        p.ready.wait()
        error = p.error if pause is None else f"❗ poll error, retry in {pause} s"
        answers.setdefault(domain, {})[contour] = p.rows, error
        if age is not None:
            ages.setdefault(domain, {})[contour] = age
    messages, outcomes = [], {}
    for domain, contours in answers.items():
        entries = [(c, s, r) for c, (rows, _) in contours.items() for s, r in (rows or {}).items()
                   if not service_view or s == service_view]
        errors = [(c, e) for c, (_, e) in contours.items() if e]
        counts = Counter(r.status for _, _, r in entries)
        if errors:
            counts["poll_error"] = len(errors)
        outcomes[domain] = dict(counts)
        if not entries and len(errors) == 1:
            messages.append(errors[0][1])
            continue
        label = domain.upper() + (f" {service_view}" if service_view else "")
        lines = [summary(label, entries, counts)]
        if domain_view:
            if service_view:
                icons = {c: ICONS[r.status] for c, _, r in entries}
                lines.append(table(["contour", service_view], [[c, icons.get(c, "—")] for c in contours]))
            else:
                services = dict.fromkeys(s for _, s, _ in entries)
                icons = {(c, s): ICONS[r.status] for c, s, r in entries}
                lines.append(table(["service", *contours],
                                   [[s, *(icons.get((c, s), "—") for c in contours)] for s in services]))
            reasons = [f"- {s} {c}: {r.reason}" for c, s, r in entries if r.reason and r.status in ("fail", "off")]
            if reasons:
                lines.append("\n".join(reasons))
        else:
            problems = [[c, s, f"{ICONS[r.status]} {r.reason}"] for c, s, r in entries
                        if r.status in ("fail", "off", "unreachable")]
            if problems:
                lines.append(table(["contour", "service", ""], problems))
        if service_view:
            det = [(c, dict(r.details)) for c, _, r in entries if r.details]
            if det:
                cols = list(dict.fromkeys(label for _, d in det for label in d))
                lines.append(table(["contour", *cols], [[c, *(str(d.get(col, "—")) for col in cols)] for c, d in det]))
        lines.extend(f"{c}: {e}" for c, e in errors)
        lines.extend(f"{c}: checked {a} s ago" for c, a in ages.get(domain, {}).items())
        messages.append("\n\n".join(lines))
    if not domain_view and answers:
        messages.append(f"Details — `/health {next(iter(answers)).upper()}` · checks that services answer, not that their "
                        "data is fresh.")
    return "\n\n".join(messages), outcomes


def selection(config: Dict[str, dict], args: List[str]):
    """-> (domains to poll, domain, service, help text or None). Arguments only pick names from the file."""
    domain = args[0] if args else None
    service = args[1] if len(args) > 1 else None
    if not config:
        return {}, domain, service, "No domains are configured for checks."
    hint = ("Domains: " + ", ".join(d.upper() for d in config)
            + f". Example: `/health {next(iter(config)).upper()}` · checks that services answer.")
    if len(args) > 2 or (domain and domain not in config):
        return {}, domain, service, (f"No domain `{domain.upper()}`. " if domain and domain not in config else "") + hint
    if service and not any(service in s for s in config[domain].values()):
        names = dict.fromkeys(n for s in config[domain].values() for n in s)
        return {}, domain, service, "Unknown service. Services: " + ", ".join(names) + ". " + hint
    return ({domain: config[domain]} if domain else config), domain, service, None


# ── the delayed answer
def _attempt(address: urllib.parse.SplitResult, deadline: float):
    expires = min(deadline, time.monotonic() + POST_ATTEMPT_S)
    remaining = expires - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    conn = probe.connection(address, remaining)
    conn.connect()
    remaining = expires - time.monotonic()
    if remaining <= 0:
        conn.close()
        raise TimeoutError()
    conn.sock.settimeout(remaining)
    timer = threading.Timer(remaining, probe.shutdown, args=(conn.sock,))
    timer.daemon = True
    timer.start()
    return conn, timer


def post(log, address, path: str, text: str, deadline: float, *, retry: bool, claim: Optional[Callable[[], bool]] = None):
    data = json.dumps({"response_type": "ephemeral", "text": text}, ensure_ascii=False).encode()
    for attempt in range(2 if retry else 1):
        sent = False
        try:
            conn, timer = _attempt(address, deadline)
            try:
                if claim is not None and not claim():
                    return
                sent = True
                conn.request("POST", path, data, {"Content-Type": "application/json", "Connection": "close"})
                with conn.getresponse() as r:
                    if not 200 <= r.status < 300:
                        log(f"health: delayed answer: HTTP {r.status}")
                return
            finally:
                timer.cancel()
                conn.close()
        except (OSError, ValueError, http.client.HTTPException) as x:
            # once the POST started its delivery is unknown; a retry is safe only before that
            if retry and attempt == 0 and not sent and isinstance(x, OSError) and time.monotonic() < deadline:
                continue
            log(f"health: delayed answer: {type(x).__name__}")
            return


class Call:
    def __init__(self, health: Health, address, path: str, uid: str, args: List[str]):
        self.health, self.address, self.path, self.uid, self.args = health, address, path, uid, args
        self.log = health.log
        self.claimed = False
        self.new: List[Tuple[Poll, dict]] = []
        self.selected: dict = {}
        self.domain = self.service = None
        self.received = time.monotonic()
        self.expires = self.received + health.deadline
        self.timer: Optional[threading.Timer] = None

    def reserve(self, domains, domain, service) -> None:
        """Called from ``start``, after the immediate answer went out: a call whose answer was never sent reserves no
        poll another call could join and wait on."""
        self.domain, self.service = domain, service
        with self.health.lock:
            self.selected, self.new, expired = self.health.reserve(domains, self.received, self.expires,
                                                                   time.monotonic())
        for rec in expired:
            if rec:
                audit(self.log, "poll", **rec)

    def audit(self, outcome: str, domains=None) -> None:
        audit(self.log, "call", user_id=self.uid, args=self.args, outcome=outcome, domains=domains or {})

    def start(self, domains=None, domain=None, service=None) -> None:
        """After the immediate answer is sent: the polls of this call, the watcher, the report."""
        self.reserve(domains or {}, domain, service)
        self.timer = threading.Timer(max(0.0, self.expires - time.monotonic()), self.expire)
        self.timer.daemon = True
        try:
            self.timer.start()
            for p, services in self.new:
                threading.Thread(target=self.health.run_poll, args=(p, services), daemon=True).start()
            threading.Thread(target=self.run, daemon=True).start()
        except RuntimeError as x:              # no more threads
            self.fail(x)

    def claim(self, outcome: str, domains) -> bool:
        with self.health.lock:
            if self.claimed or time.monotonic() >= self.expires:
                return False
            self.claimed = True
        if self.timer:
            self.timer.cancel()
        self.audit(outcome, domains)
        return True

    def expire(self) -> None:
        with self.health.lock:
            if self.claimed:
                return
            self.claimed = True
            now = time.monotonic()
            recs = [p.finish(now, error=TIMEOUT) for p, _ in self.new]
        for rec in recs:
            if rec:
                audit(self.log, "poll", **rec)
        self.audit("timeout")
        post(self.log, self.address, self.path, TIMEOUT, now + POST_ATTEMPT_S, retry=False)   # one try is left

    def finish(self, text: str, *, outcome: str, domains=None, error_class: str = "") -> None:
        with self.health.lock:
            if self.claimed:
                return
            now = time.monotonic()
            recs = [p.finish(now, error=text, error_class=error_class) for p, _ in self.new] if error_class else []
        for rec in recs:
            if rec:
                audit(self.log, "poll", **rec)
        if now >= self.expires:
            self.expire()
            return
        post(self.log, self.address, self.path, text, self.expires, retry=True,
             claim=lambda: self.claim(outcome, domains))

    def fail(self, x: BaseException) -> None:
        self.log(f"health: poll error ({type(x).__name__})")
        self.finish(f"❗ poll error: {type(x).__name__}", outcome="error", error_class=type(x).__name__)

    def run(self) -> None:
        try:
            text, outcomes = report(self.selected, self.domain, self.service)
        except Exception as x:
            self.fail(x)
        else:
            self.finish(text, outcome="completed", domains=outcomes)


# ── the handler
_STATE: Dict[str, Health] = {}


def seconds(edge, name: str, default: float) -> float:
    """A positive finite number of seconds from the edge's environment; anything else — the default, said in the log."""
    raw = edge.env.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
    except ValueError:
        v = float("nan")
    if not (0 < v < 3600):
        edge.log(f"health: {name} is not a number of seconds in (0, 3600) — {default:g} is used")
        return default
    return v


def state(edge) -> Health:
    h = _STATE.get(id(edge))
    if h is None:
        h = _STATE[id(edge)] = Health(edge.log, min_gap=seconds(edge, "EDGE_HEALTH_MIN_GAP_S", 10),
                                      deadline=seconds(edge, "EDGE_HEALTH_DEADLINE_S", 5),
                                      probe_s=seconds(edge, "EDGE_HEALTH_PROBE_S", 1))
    return h


def handler(call) -> str:
    edge = call.edge
    h = state(edge)
    args = [a.lower() for a in call.args]
    log = edge.log

    def refuse(outcome: str, text: str) -> str:
        audit(log, "call", user_id=call.user_id, args=[], arg_count=len(args), outcome=outcome, domains={})
        return text

    if edge.env.get("EDGE_HEALTH", "").strip().lower() == "off":
        return refuse("disabled", OFF)
    roles = (call.caller or {}).get("roles")
    if not isinstance(roles, str) or not roles.split() or "system_guest" in roles.split():
        return refuse("denied", MEMBERS)
    try:
        address = origin(edge.mm_url, with_path=True)
        path = urllib.parse.urlsplit(call.form.get("response_url", "")).path
    except ValueError:
        return refuse("invalid_response_url", BAD_URL)
    prefix = address.path.rstrip("/")
    if not path.startswith(prefix) or not HOOK.fullmatch(path[len(prefix):]):
        return refuse("invalid_response_url", BAD_URL)
    config = h.config(edge.env.get("EDGE_HEALTH_TARGETS"))
    domains, domain, service, hint = selection(config, args)
    if hint:
        return refuse("invalid_arguments" if config else "unconfigured", hint)
    c = Call(h, address, path, call.user_id, args)
    edge.after.append(lambda: c.start(domains, domain, service))
    return CHECKING
