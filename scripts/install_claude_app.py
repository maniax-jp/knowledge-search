#!/usr/bin/env python3
"""Set up Claude.app on this Mac (chat, Cowork, Code tab) as a knowledge-search client.

    python3 scripts/install_claude_app.py --label host-a-claude [--api-url http://<server>:9470] [--env-file <file>]

Stdlib only. Idempotent; re-running resumes. What it does:
  1. Resolve the API key (env, client.env, --env-file, or ~/.hermes/.env).
  2. Write KS_API_URL / KS_API_KEY / KS_HOST_LABEL into ~/.config/knowledge-search/client.env (chmod 600).
  3. Check the API and that the label is not already used by another machine.
  4. Register MCP server `knowledge-search` (stdio bridge -> SSE) in claude_desktop_config.json
     (chat, Cowork and the Code tab) and in Claude Code's user scope (terminal `claude`),
     then check that the bridge lists the tools.
  5. Save this Mac's past Claude Code / Cowork sessions once.
  6. Create the LaunchAgent that saves new messages every 10 minutes.

Restart Claude.app afterwards so the MCP tools load.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install_client import LABEL_RE, LEDGER_ENV, api, die, read_env, write_env  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
KS_HOME = Path(os.environ.get("KS_HOME") or Path.home() / ".config" / "knowledge-search")
ENV_FILE = KS_HOME / "client.env"
STATE = KS_HOME / "claude_autosave.json"
LOGS = KS_HOME / "logs"
DESKTOP_CONFIG = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
BRIDGE = ROOT / "scripts" / "mcp_stdio_bridge.py"
AGENT_LABEL = "local.knowledge-search.claude-autosave"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
REGISTER_LABEL = "local.knowledge-search.claude-register"
REGISTER_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{REGISTER_LABEL}.plist"
SERVER = "knowledge-search"


def step(n: int, msg: str) -> None:
    print(f"[{n}/6] {msg}", flush=True)


def python() -> str:
    # The real interpreter, not a pyenv shim: launchd and Claude.app start with a bare PATH.
    return os.path.realpath(sys.executable)


def claude_running() -> bool:
    ps = subprocess.run(["ps", "-axo", "comm"], capture_output=True, text=True).stdout
    return any(line.endswith("/Claude.app/Contents/MacOS/Claude") for line in ps.splitlines())


def schedule_desktop_register() -> None:
    """Claude.app rewrites claude_desktop_config.json from memory, so an edit made while
    it runs is lost. A one-shot LaunchAgent (outside the app's process tree) waits for
    Cmd+Q, writes the entry, and reopens the app."""
    LOGS.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": REGISTER_LABEL,
        "ProgramArguments": [python(), str(Path(__file__).resolve()), "--register-desktop-when-quit"],
        "RunAtLoad": True,
        "StandardOutPath": str(LOGS / "claude_register.log"),
        "StandardErrorPath": str(LOGS / "claude_register.log"),
    }
    REGISTER_PLIST.write_bytes(plistlib.dumps(plist))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{REGISTER_LABEL}"], capture_output=True)
    proc = subprocess.run(["launchctl", "bootstrap", domain, str(REGISTER_PLIST)], capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"launchctl bootstrap failed: {(proc.stdout + proc.stderr).strip()}")


def register_desktop_when_quit() -> None:
    entry = {"command": python(), "args": [str(BRIDGE)]}
    deadline = time.time() + 7 * 86400
    while claude_running() and time.time() < deadline:
        time.sleep(2)
    time.sleep(3)  # let the app finish its last write
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    if claude_running():
        print(f"{stamp} gave up: Claude.app did not quit")
    else:
        print(f"{stamp} {write_desktop(entry)}", flush=True)
        subprocess.run(["open", "-a", "Claude"])
    REGISTER_PLIST.unlink(missing_ok=True)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{REGISTER_LABEL}"])  # ends this process


def register_desktop(entry: dict) -> str:
    cfg = json.loads(DESKTOP_CONFIG.read_text(encoding="utf-8")) if DESKTOP_CONFIG.is_file() else {}
    if cfg.get("mcpServers", {}).get(SERVER) == entry:
        return "already registered"
    if claude_running():
        schedule_desktop_register()
        return "pending: quit Claude.app (Cmd+Q); it is added then and Claude.app reopens"
    return write_desktop(entry)


def write_desktop(entry: dict) -> str:
    cfg = json.loads(DESKTOP_CONFIG.read_text(encoding="utf-8")) if DESKTOP_CONFIG.is_file() else {}
    servers = cfg.setdefault("mcpServers", {})
    if servers.get(SERVER) == entry:
        return "already registered"
    if DESKTOP_CONFIG.is_file():
        backup = DESKTOP_CONFIG.with_name(f"{DESKTOP_CONFIG.name}.bak.{time.strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(DESKTOP_CONFIG, backup)
    servers[SERVER] = entry
    DESKTOP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = DESKTOP_CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(DESKTOP_CONFIG)
    return "registered"


def register_claude_code(entry: dict) -> str:
    exe = shutil.which("claude")
    if not exe:
        return "claude CLI not found (skipped)"
    got = subprocess.run([exe, "mcp", "get", SERVER], capture_output=True, text=True)
    if got.returncode == 0:
        return "already registered"
    proc = subprocess.run([exe, "mcp", "add", "--scope", "user", SERVER, "--", entry["command"], *entry["args"]],
                          capture_output=True, text=True)
    return "registered" if proc.returncode == 0 else f"failed: {(proc.stdout + proc.stderr).strip()[-200:]}"


def test_bridge(entry: dict) -> list[str]:
    """initialize + tools/list through the bridge, as Claude.app would."""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "install_claude_app", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.Popen([entry["command"], *entry["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        for m in msgs:
            proc.stdin.write(json.dumps(m) + "\n")
            proc.stdin.flush()
        deadline = time.time() + 30
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            reply = json.loads(line)
            if reply.get("id") == 2:
                if "error" in reply:
                    die(f"bridge tools/list failed: {reply['error'].get('message')}")
                return [t["name"] for t in reply["result"]["tools"]]
        die("bridge did not answer tools/list:\n" + (proc.stderr.read() if proc.poll() is not None else ""))
    finally:
        proc.kill()
    return []


def install_agent(interval: int) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [python(), str(ROOT / "scripts" / "claude_autosave.py")],
        "StartInterval": interval,
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": str(LOGS / "claude_autosave.launchd.log"),
        "StandardErrorPath": str(LOGS / "claude_autosave.launchd.log"),
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(plist))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{AGENT_LABEL}"], capture_output=True)
    proc = subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"launchctl bootstrap failed: {(proc.stdout + proc.stderr).strip()}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", default="", help="unique label for this Mac's Claude.app, e.g. host-a-claude")
    p.add_argument("--api-url", default="", help="API base, e.g. http://<server>:9470 (default: existing value or the key file)")
    p.add_argument("--mcp-url", default="", help="MCP SSE URL (default: API host, port 9471, /sse)")
    p.add_argument("--env-file", type=Path, help="file holding KS_API_KEY (and optionally KS_API_URL)")
    p.add_argument("--interval", type=int, default=600, help="autosave interval in seconds")
    p.add_argument("--adopt", action="store_true", help="the label already has data and it is this Mac's")
    p.add_argument("--skip-initial", action="store_true", help="do not save past sessions now")
    p.add_argument("--register-desktop-when-quit", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()

    if args.register_desktop_when_quit:
        register_desktop_when_quit()
        return
    if sys.platform != "darwin":
        die("this installer is for Claude.app on macOS")
    label = args.label.strip().lower()
    if not label:
        die("--label is required")
    if not LABEL_RE.match(label):
        die("label must match [a-z0-9][a-z0-9-]{0,31}")

    step(1, "API key")
    current = read_env(ENV_FILE)
    extra: dict[str, str] = {}
    cands = ([args.env_file] if args.env_file else []) + [Path.home() / ".hermes" / ".env"] + LEDGER_ENV
    for cand in cands:
        if cand and read_env(cand).get("KS_API_KEY"):
            extra = read_env(cand)
            break
    key = os.environ.get("KS_API_KEY") or current.get("KS_API_KEY") or extra.get("KS_API_KEY", "")
    if not key:
        die("KS_API_KEY not found. Pass --env-file with a file that holds it; "
            f"looked in {ENV_FILE} and {', '.join(map(str, cands))}")
    base = (args.api_url or current.get("KS_API_URL") or extra.get("KS_API_URL") or "").rstrip("/")
    if not base:
        die("API URL unknown. Pass --api-url http://<server>:9470 (or put KS_API_URL in the --env-file)")
    host = urllib.parse.urlsplit(base).hostname or "127.0.0.1"
    mcp_url = args.mcp_url or current.get("KS_MCP_URL") or f"http://{host}:9471/sse"
    print(f"      api={base} mcp={mcp_url} label={label}")

    step(2, f"write {ENV_FILE}")
    write_env(ENV_FILE, {"KS_API_URL": base, "KS_MCP_URL": mcp_url, "KS_API_KEY": key, "KS_HOST_LABEL": label})
    os.chmod(KS_HOME, 0o700)

    step(3, "check API and label")
    try:
        stats = api(base, key, "/v1/stats")
    except urllib.error.HTTPError as exc:
        die(f"API rejected the key or failed: HTTP {exc.code}")
    except OSError as exc:
        die(f"API unreachable at {base}: {exc}")
    print(f"      opensearch={stats.get('opensearch')} chunks={stats.get('chunks')}")
    used = api(base, key, "/v1/documents", {"kind": "chat", "agent": "claude", "tag": label, "limit": 1})
    if used.get("total") and not STATE.is_file() and not args.adopt:
        die(f"label '{label}' already has {used['total']} chat chunks from another install. "
            "Pick another label, or pass --adopt if that data came from this Mac.")

    step(4, f"MCP server {SERVER}")
    entry = {"command": python(), "args": [str(BRIDGE)]}
    print(f"      Claude.app: {register_desktop(entry)} ({DESKTOP_CONFIG})")
    print(f"      Claude Code user scope: {register_claude_code(entry)}")
    print(f"      tools: {', '.join(test_bridge(entry))}")

    step(5, "save past sessions" + (" (skipped)" if args.skip_initial else ""))
    if not args.skip_initial:
        proc = subprocess.run([python(), str(ROOT / "scripts" / "ingest_claude_sessions.py"),
                               "--host", label, "--state", str(STATE)])
        if proc.returncode != 0:
            die("initial save failed; fix the error above and re-run (it resumes)")

    step(6, f"LaunchAgent {AGENT_LABEL} (every {args.interval}s)")
    install_agent(args.interval)
    print(f"      {PLIST}")

    if REGISTER_PLIST.is_file():
        print("\nDone. Quit Claude.app with Cmd+Q: the MCP entry is written after it exits and "
              "Claude.app reopens with the knowledge-search tools.")
    else:
        print("\nDone. Quit and reopen Claude.app (Cmd+Q) so the knowledge-search tools load.")


if __name__ == "__main__":
    main()
