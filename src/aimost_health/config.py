"""The targets file (``EDGE_HEALTH_TARGETS``, JSON): domain → contour → service → probe or ``null`` (health spec).

A probe::

    {"url": "http://10.0.0.5:8080",          # scheme, host, port — nothing else (no path, user, query, fragment)
     "path": "/health",                       # absolute, [A-Za-z0-9/_.~-], no "." / ".." segments
     "rule": "http" | "status" | "role",
     "redirect": {"status": 307, "prefix": "/app/"},           # rule http only: an expected redirect, never followed
     "mode": {"field": "runMode", "expected": "live", "allowed": ["live", "off"], "off_status": false},
     "reasons": {"<reason the service reports>": "<short text shown>"},
     "details": [{"field": "queue.depth", "label": "queue", "count": true},
                 {"field": "source", "label": "source", "allowed": ["a", "b"]}]}

Everything the chat shows comes from this file or from a fixed list of words: names of domains, contours and services must
match ``NAME``, shown texts (labels, reasons) — ``TEXT`` (no mentions, links, table breaks or code marks). A value from a
response body reaches the chat only through ``allowed`` lists and ``count`` fields. A file that does not pass is rejected
whole, with the reason (never its content) in the log, and the last accepted version stays.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from typing import Callable, Dict, Optional, Tuple

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
PATH = re.compile(r"/[A-Za-z0-9/_.~-]*")
FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}(?:\.[A-Za-z][A-Za-z0-9_]{0,31}){0,3}")
TEXT = re.compile(r"[^\W_][\w .,;()+'-]{0,79}")
RULES = ("http", "status", "role")
KEYS = {"url", "path", "rule", "redirect", "mode", "reasons", "details"}
MAX_DETAILS = 8


class ConfigError(ValueError):
    """Why the file was refused — never its content."""


def good_path(value) -> bool:
    return (isinstance(value, str) and bool(PATH.fullmatch(value)) and not value.startswith("//")
            and not {".", ".."}.intersection(value.split("/")))


def origin(url, *, with_path: bool = False) -> urllib.parse.SplitResult:
    """A probe address (scheme://host[:port]); ``with_path`` — the chat server's own URL, which may carry a path prefix."""
    if not isinstance(url, str) or re.search(r"\s|[\\\x00-\x1f\x7f]", url):
        raise ValueError("bad address")
    a = urllib.parse.urlsplit(url)
    try:
        port = a.port
    except ValueError:
        raise ValueError("bad address") from None
    path_ok = a.path in ("", "/") or (with_path and good_path(a.path))
    if (a.scheme not in ("http", "https") or not a.hostname or a.username is not None or a.password is not None
            or not path_ok or a.query or a.fragment or port == 0):
        raise ValueError("bad address")
    return a


def _text(value) -> bool:
    return isinstance(value, str) and bool(TEXT.fullmatch(value))


def _probe(target) -> None:
    if (not isinstance(target, dict) or target.get("rule") not in RULES or not good_path(target.get("path"))
            or set(target) - KEYS):
        raise ConfigError("bad probe")
    try:
        origin(target.get("url"))
    except ValueError:
        raise ConfigError("bad probe address") from None
    if "redirect" in target:
        r = target["redirect"]
        if (target["rule"] != "http" or not isinstance(r, dict) or set(r) != {"status", "prefix"}
                or type(r["status"]) is not int or not 300 <= r["status"] < 400 or not good_path(r["prefix"])):
            raise ConfigError("bad redirect rule")
    if "mode" in target:
        m = target["mode"]
        if (target["rule"] == "http" or not isinstance(m, dict)
                or not {"field", "expected", "allowed"} <= set(m) <= {"field", "expected", "allowed", "off_status"}
                or not isinstance(m["field"], str) or not FIELD.fullmatch(m["field"]) or "." in m["field"]
                or not isinstance(m["allowed"], list) or not m["allowed"]
                or any(not isinstance(v, str) or not NAME.fullmatch(v) for v in m["allowed"])
                or m["expected"] not in m["allowed"] or type(m.get("off_status", False)) is not bool):
            raise ConfigError("bad mode rule")
    if "reasons" in target:
        r = target["reasons"]
        if (target["rule"] == "http" or not isinstance(r, dict)
                or any(not isinstance(k, str) or len(k) > 300 or not _text(v) for k, v in r.items())):
            raise ConfigError("bad reasons")
    if "details" in target:
        d = target["details"]
        if target["rule"] == "http" or not isinstance(d, list) or len(d) > MAX_DETAILS:
            raise ConfigError("bad details")
        for x in d:
            ok = (isinstance(x, dict) and isinstance(x.get("field"), str) and bool(FIELD.fullmatch(x["field"]))
                  and _text(x.get("label")) and (set(x) == {"field", "label", "count"} and x["count"] is True
                                                 or set(x) == {"field", "label", "allowed"}
                                                 and isinstance(x["allowed"], list) and x["allowed"]
                                                 and all(isinstance(v, str) and NAME.fullmatch(v) for v in x["allowed"])))
            if not ok:
                raise ConfigError("bad details")


def validate(config) -> None:
    def entries(value):
        if not isinstance(value, dict):
            raise ConfigError("an object expected")
        if any(not isinstance(k, str) or not NAME.fullmatch(k) for k in value):
            raise ConfigError("bad name of a domain, contour or service")
        return value.items()

    for _, contours in entries(config):
        if not contours:
            raise ConfigError("a domain without contours")
        for _, services in entries(contours):
            if not services:
                raise ConfigError("a contour without services")
            for _, target in entries(services):
                if target is not None:
                    _probe(target)


class Targets:
    """The file, re-read when it changes (mtime, inode, size); a refused version keeps the last accepted one."""

    def __init__(self, log: Callable[[str], None]):
        self.log = log
        self.config: Dict[str, dict] = {}
        self.stamp: Optional[Tuple] = None

    def load(self, path: Optional[str]) -> Dict[str, dict]:
        try:
            if not path:
                raise FileNotFoundError()
            st = os.stat(path)
            stamp = (path, st.st_mtime_ns, st.st_ino, st.st_size)
            if stamp == self.stamp:
                return self.config
            self.stamp = stamp
            with open(path, encoding="utf-8") as f:
                config = json.load(f)
            validate(config)
        except FileNotFoundError:
            self.stamp, self.config = None, {}
            return self.config
        except (OSError, ValueError, RecursionError) as x:
            # system errors may carry the path or a line of the file: only the class goes to the log
            why = str(x) if isinstance(x, ConfigError) else type(x).__name__
            self.log(f"health: targets refused ({why}), the previous ones stay")
            return self.config
        self.config = config
        return config
