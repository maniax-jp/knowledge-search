"""Run the HTTP API, wait until it answers, then start the MCP SSE server."""

from __future__ import annotations

import os
import time
import urllib.request
from multiprocessing import Process

import uvicorn

from app.mcp_server import main as run_mcp


def _api() -> None:
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("KS_API_PORT", "9470")), log_level="info")


def _wait_api() -> None:
    url = "http://127.0.0.1:9470/health"
    last = "not started"
    for _ in range(100):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return
                last = f"HTTP {resp.status}"
        except Exception as exc:  # noqa: BLE001 - boot wait
            last = str(exc)
        time.sleep(3)
    raise SystemExit(f"API did not become ready: {last}")


def main() -> None:
    api = Process(target=_api, daemon=True)
    api.start()
    _wait_api()
    mcp = Process(target=run_mcp, daemon=True)
    mcp.start()
    while api.is_alive() and mcp.is_alive():
        time.sleep(5)
    raise SystemExit("api or mcp process exited")


if __name__ == "__main__":
    main()
