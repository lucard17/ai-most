"""Minimal Mattermost websocket client on the standard library: the bridge's alarm clock instead of frequent polling.

A websocket event only says "something happened for this bot"; the bridge then fetches posts for that bot alone. This keeps
load far below the server's rate limit when many people run bridges.

    Watcher(url, token, on_event).start()   thread: connect to /api/v4/websocket, authentication_challenge, read frames;
                                            on_event(event_name) per event, "hello" after every (re)connect;
                                            reconnect with backoff 1 → 60 s; ping every AIMOST_WS_PING_S (30 s).

Frames follow RFC 6455: the client masks, the server does not; text, ping/pong, close; fragments are joined. No compression.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import threading
import time
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse


def _ping_s() -> float:
    return float(os.environ.get("AIMOST_WS_PING_S", "30"))


class Closed(Exception):
    pass


class WS:
    def __init__(self, url: str, token: str, timeout: float = 60):
        u = urlparse(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        self.s = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) if u.scheme == "https" else raw
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path.rstrip("/") or "") + "/api/v4/websocket"
        req = (f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer {token}\r\n\r\n")
        self.s.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            b = self.s.recv(1)
            if not b:
                raise Closed("server closed the connection during the handshake")
            head += b
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            raise Closed(f"handshake: {status}")
        self.buf = b""
        self.part = b""
        self.send_json({"seq": 1, "action": "authentication_challenge", "data": {"token": token}})

    def _frame(self) -> Optional[Tuple[int, int, bytes]]:
        """A whole frame from the buffer (op, fin, data), or None if more bytes are needed. The buffer is left untouched
        until a frame is complete, so a read timeout in the middle of a frame does not break parsing."""
        b = self.buf
        if len(b) < 2:
            return None
        fin, op, n, i = b[0] & 0x80, b[0] & 0x0F, b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, i = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, i = struct.unpack(">Q", b[2:10])[0], 10
        mask = None
        if b[1] & 0x80:
            if len(b) < i + 4:
                return None
            mask, i = b[i:i + 4], i + 4
        if len(b) < i + n:
            return None
        data = b[i:i + n]
        self.buf = b[i + n:]
        if mask:
            data = bytes(x ^ mask[k % 4] for k, x in enumerate(data))
        return op, fin, data

    def send(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_json(self, obj: object) -> None:
        self.send(0x1, json.dumps(obj).encode())

    def recv(self) -> Optional[str]:
        """The next text message, or None for a control frame (ping is answered here). ``socket.timeout`` propagates;
        unread bytes stay in the buffer."""
        while True:
            f = self._frame()
            if f is None:
                b = self.s.recv(65536)
                if not b:
                    raise Closed("connection closed")
                self.buf += b
                continue
            op, fin, data = f
            if op == 0x8:
                raise Closed("server sent close")
            if op == 0x9:
                self.send(0xA, data)
                return None
            if op == 0xA:
                return None
            self.part += data
            if fin:
                m, self.part = self.part, b""
                return m.decode(errors="replace")

    def close(self) -> None:
        for fn in (lambda: self.send(0x8), self.s.close):
            try:
                fn()
            except Exception:
                pass


class Watcher(threading.Thread):
    def __init__(self, url: str, token: str, on_event: Callable[[str], None],
                 log: Callable[[str], None] = lambda m: None, name: str = "ws"):
        super().__init__(daemon=True, name=name)
        self.url, self.token, self.on_event, self.log = url, token, on_event, log
        self.connected = False
        self.stopped = threading.Event()

    def stop(self) -> None:
        self.stopped.set()

    def run(self) -> None:
        pause = 1.0
        while not self.stopped.is_set():
            ws = None
            try:
                ws = WS(self.url, self.token, timeout=_ping_s())
                self.connected, pause = True, 1.0
                self.on_event("hello")  # after a (re)connect — one pass for whatever was missed meanwhile
                last = time.time()
                while not self.stopped.is_set():
                    try:
                        m = ws.recv()
                    except socket.timeout:
                        m = None
                    if time.time() - last >= _ping_s():
                        ws.send(0x9)
                        last = time.time()
                    if m:
                        try:
                            ev = json.loads(m).get("event")
                        except ValueError:
                            ev = None
                        if ev:
                            self.on_event(ev)
            except Exception as e:
                self.log(f"websocket: {e} — reconnect in {pause:g} s")
            finally:
                self.connected = False
                if ws:
                    ws.close()
            self.stopped.wait(pause)
            pause = min(pause * 2, 60.0)
