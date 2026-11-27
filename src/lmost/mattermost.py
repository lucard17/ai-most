"""Mattermost transport: REST calls, files, and a short description of a post's attachments for inbox events.

Functions take a connection (``connections.Connection``) and a bot token. Tests replace ``api`` with a fake server's
URL in the connection; nothing here keeps state.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Callable, Optional

from .connections import Connection

HTTPError = urllib.error.HTTPError


def _timeout() -> float:
    return float(os.environ.get("LMOST_API_TIMEOUT", "20"))


def api(conn: Connection, method: str, path: str, body: object = None, token: Optional[str] = None) -> object:
    """``/api/v4`` call with a bot token; returns parsed JSON (None for an empty body). Raises HTTPError."""
    req = urllib.request.Request(
        conn["URL"].rstrip("/") + "/api/v4" + path, method=method,
        data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
        headers={"Authorization": "Bearer " + (token or conn.get("BOT_TOKEN", "")), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_timeout()) as r:
        return json.loads(r.read() or b"null")


def upload(conn: Connection, channel_id: str, path: str, token: str) -> str:
    """Upload a file into a channel; returns the file id to put into a post's ``file_ids``."""
    b = uuid.uuid4().hex
    name = os.path.basename(path)
    with open(path, "rb") as f:
        data = f.read()
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"channel_id\"\r\n\r\n{channel_id}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(conn["URL"].rstrip("/") + "/api/v4/files", method="POST", data=body,
                                 headers={"Authorization": "Bearer " + token,
                                          "Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["file_infos"][0]["id"]


def download(conn: Connection, file_id: str, token: str) -> bytes:
    req = urllib.request.Request(conn["URL"].rstrip("/") + f"/api/v4/files/{file_id}",
                                 headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def describe(post: dict, call: Optional[Callable[[str, str], object]] = None) -> str:
    """Tail of an inbox event for a post: attachments (name, id, size) and forwarded posts (permalink embeds)."""
    out = []
    md = post.get("metadata") or {}
    files = md.get("files") or []
    if files:
        out.append("📎 files (lmost file <id>): " + ", ".join(
            f"{f.get('name')} ({f.get('id')}, {int(f.get('size') or 0) // 1024} KB)" for f in files))
    for e in md.get("embeds") or []:
        if e.get("type") != "permalink":
            continue
        d = e.get("data") or {}
        fp = d.get("post") or {}
        who = fp.get("user_id", "?")
        if call:
            try:
                who = "@" + call("GET", f"/users/{who}")["username"]
            except Exception:
                pass
        where = d.get("channel_display_name") or d.get("channel_id", "")
        out.append(f"↪ forwarded ({who}, {where}): {fp.get('message', '')}")
        ff = (fp.get("metadata") or {}).get("files") or []
        if ff:
            out.append("  📎 in the forwarded post: " + ", ".join(f"{f.get('name')} ({f.get('id')})" for f in ff))
    return ("\n" + "\n".join(out)) if out else ""
