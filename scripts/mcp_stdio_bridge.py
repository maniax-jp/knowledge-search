#!/usr/bin/env python3
"""stdio <-> SSE bridge for the knowledge-search MCP server. Stdlib only.

Claude.app (chat, Cowork, Code tab) runs local MCP servers over stdio, and its
remote connectors are reached from Anthropic's cloud, which cannot see the LAN.
This process is the local stdio server; it relays JSON-RPC to
http://<server>:9471/sse with the Bearer key.

- KS_API_KEY / KS_MCP_URL / KS_API_URL come from the environment, then
  ~/.config/knowledge-search/client.env (written by install_claude_app.py).
  The key is never put in claude_desktop_config.json.
- If the SSE stream drops (server restart, Mac sleep), it reconnects and replays
  the client's initialize handshake, so the app keeps its session.
- Diagnostics go to stderr only (Claude.app logs it as mcp-server-*.log).
- A watchdog fails any call that has had no reply for KS_BRIDGE_WATCHDOG seconds
  (default 75) and reconnects the stream, instead of leaving the app waiting.
  Calls that were in flight when the stream dropped get an error reply too.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KS_HOME = Path(os.environ.get("KS_HOME") or Path.home() / ".config" / "knowledge-search")
REINIT_ID = "ks-bridge-reinit"
WATCHDOG = float(os.environ.get("KS_BRIDGE_WATCHDOG", "75"))


def _load_env() -> None:
    path = KS_HOME / "client.env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if k.strip().startswith("KS_"):
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def log(msg: str) -> None:
    print(f"knowledge-search bridge: {msg}", file=sys.stderr, flush=True)


class Bridge:
    def __init__(self, url: str, key: str) -> None:
        self.url = url
        self.key = key
        self.endpoint: str | None = None
        self.ready = threading.Event()
        self.out_lock = threading.Lock()
        self.init_msg: dict | None = None
        self.init_sent = False
        self.pending: dict = {}          # request id -> time sent (calls awaiting a reply)
        self.pending_lock = threading.Lock()
        self.resp = None                 # the open SSE response, closed by the watchdog

    def write(self, msg: dict) -> None:
        with self.out_lock:
            sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def _post(self, msg: dict) -> None:
        req = urllib.request.Request(
            self.endpoint or "",
            data=json.dumps(msg, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()

    def send(self, msg: dict) -> None:
        """Forward one client message; retry across a reconnect."""
        for attempt in range(4):
            if not self.ready.wait(60):
                break
            try:
                self._post(msg)
                if msg.get("method") == "initialize":
                    self.init_sent = True
                if "id" in msg and "method" in msg:
                    with self.pending_lock:
                        self.pending[msg["id"]] = time.time()
                return
            except (urllib.error.URLError, OSError) as exc:
                log(f"post failed ({exc}); retry {attempt + 1}")
                self.ready.clear()
                time.sleep(1)
        if "id" in msg and "method" in msg:
            self.write({"jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32000, "message": f"knowledge-search unreachable at {self.url}"}})

    def _dispatch(self, event: str, data: str) -> None:
        if event == "endpoint":
            self.endpoint = urllib.parse.urljoin(self.url, data.strip())
            if self.init_sent and self.init_msg:  # reconnect: the server lost our session
                try:
                    self._post({**self.init_msg, "id": REINIT_ID})
                    self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
                except (urllib.error.URLError, OSError) as exc:
                    log(f"re-initialize failed: {exc}")
                    return
            self.ready.set()
        elif event == "message":
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                log("bad message from server")
                return
            if isinstance(msg, dict) and msg.get("id") == REINIT_ID:
                return
            if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                with self.pending_lock:
                    self.pending.pop(msg["id"], None)
            self.write(msg)

    def fail_pending(self, why: str) -> None:
        """Answer every in-flight call with an error so the app stops waiting."""
        with self.pending_lock:
            ids, self.pending = list(self.pending), {}
        for mid in ids:
            self.write({"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32000, "message": f"knowledge-search: {why}"}})
        if ids:
            log(f"failed {len(ids)} pending call(s): {why}")

    def watchdog_loop(self) -> None:
        while True:
            time.sleep(5)
            with self.pending_lock:
                oldest = min(self.pending.values(), default=None)
            if oldest is not None and time.time() - oldest > WATCHDOG:
                log(f"no reply for {WATCHDOG:.0f}s; reconnecting")
                self.fail_pending(f"no reply within {WATCHDOG:.0f}s")
                self._drop_stream()

    def _drop_stream(self) -> None:
        """Shut the SSE socket so the blocked read in sse_loop fails and reconnects.

        Not resp.close(): that takes the reader's buffer lock, which the reading
        thread holds, and deadlocks."""
        resp = self.resp
        if resp is None:
            return
        try:
            sock = resp.fp.raw._sock  # urllib: HTTPResponse -> BufferedReader -> SocketIO
            sock.shutdown(socket.SHUT_RDWR)
        except Exception as exc:  # noqa: BLE001
            log(f"could not shut the stream socket: {exc}")

    def sse_loop(self) -> None:
        backoff = 1.0
        while True:
            req = urllib.request.Request(
                self.url, headers={"Authorization": f"Bearer {self.key}", "Accept": "text/event-stream"})
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    self.resp = resp
                    backoff = 1.0
                    event, data = "message", []
                    for raw in resp:
                        line = raw.decode("utf-8").rstrip("\r\n")
                        if not line:
                            if data:
                                self._dispatch(event, "\n".join(data))
                            event, data = "message", []
                        elif line.startswith(":"):
                            continue
                        else:
                            field, _, value = line.partition(":")
                            value = value[1:] if value.startswith(" ") else value
                            if field == "event":
                                event = value
                            elif field == "data":
                                data.append(value)
                log("stream closed")
            except urllib.error.HTTPError as exc:
                log(f"HTTP {exc.code} from {self.url}")
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log(f"stream error: {exc}")
            self.resp = None
            self.ready.clear()
            self.endpoint = None
            self.fail_pending("stream dropped, replies lost")
            time.sleep(backoff)
            backoff = min(30.0, backoff * 2)

    def stdin_loop(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log("bad message from client")
                continue
            if isinstance(msg, dict) and msg.get("method") == "initialize":
                self.init_msg = msg
            self.send(msg)


def main() -> None:
    _load_env()
    key = os.environ.get("KS_API_KEY", "")
    if not key:
        log(f"KS_API_KEY missing; run install_claude_app.py (looked in {KS_HOME / 'client.env'})")
        sys.exit(1)
    url = os.environ.get("KS_MCP_URL", "")
    if not url and os.environ.get("KS_API_URL"):
        url = f"http://{urllib.parse.urlsplit(os.environ['KS_API_URL']).hostname}:9471/sse"
    if not url:
        log(f"KS_MCP_URL missing; run install_claude_app.py (looked in {KS_HOME / 'client.env'})")
        sys.exit(1)
    bridge = Bridge(url, key)
    threading.Thread(target=bridge.sse_loop, daemon=True).start()
    threading.Thread(target=bridge.watchdog_loop, daemon=True).start()
    bridge.stdin_loop()  # returns when the app closes stdin


if __name__ == "__main__":
    main()
