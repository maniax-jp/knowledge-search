#!/usr/bin/env python3
"""Set up one Hermes machine as a knowledge-search client. Stdlib only. Idempotent.

    python3 scripts/install_client.py --label host-a --api-url http://<server>:9470 --env-file <file with KS_API_KEY>

What it does (re-running is safe):
  1. Resolve the API key (env, ~/.hermes/.env, --env-file, the repo .env on the server, or
     ~/.config/knowledge-search/client.env).
  2. Write KS_API_URL / KS_API_KEY / KS_HOST_LABEL into ~/.hermes/.env (chmod 600).
  3. Check the API and that the label is not already used by another machine.
  4. Install the knowledge-search skill into ~/.hermes/skills/devops/.
  5. Register MCP server `knowledge-search` (SSE + Bearer header) in config.yaml.
  6. Save this machine's past sessions once (full history, incremental state).
  7. Create the Hermes cron job `knowledge_autosave` (no_agent, every 10m).

It does not restart the gateway; do that afterwards so the MCP tools load.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
ENV_FILE = HERMES_HOME / ".env"
STATE = HERMES_HOME / "state" / "knowledge_autosave.json"
STUB = HERMES_HOME / "scripts" / "knowledge_autosave.py"
SKILL_SRC = ROOT / "hermes" / "skills" / "knowledge-search" / "SKILL.md"
SKILL_DST = HERMES_HOME / "skills" / "devops" / "knowledge-search" / "SKILL.md"
LEDGER_ENV = [
    ROOT / ".env",  # the server host keeps the key here
    Path.home() / ".config" / "knowledge-search" / "client.env",
]
CRON_NAME = "knowledge_autosave"
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def step(n: int, msg: str) -> None:
    print(f"[{n}/7] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def write_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={values[key]}"
            done.add(key)
    new = [f"{k}={v}" for k, v in values.items() if k not in done]
    if new:
        lines += ["", "# knowledge-search client (scripts/install_client.py)"] + new
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def api(base: str, key: str, path: str, query: dict | None = None) -> dict:
    url = base.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def hermes(*args: str, check: bool = True) -> str:
    exe = shutil.which("hermes")
    if not exe:
        die("hermes CLI not found on PATH")
    proc = subprocess.run([exe, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        die(f"hermes {' '.join(args[:2])} failed:\n{proc.stdout}{proc.stderr}")
    return proc.stdout + proc.stderr


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", required=True, help="unique machine label, e.g. host-a (used in source_id)")
    p.add_argument("--api-url", default="", help="API base, e.g. http://<server>:9470 (default: existing value or the key file)")
    p.add_argument("--mcp-url", default="", help="MCP SSE URL (default: API host, port 9471, /sse)")
    p.add_argument("--env-file", type=Path, help="file holding KS_API_KEY (and optionally KS_API_URL)")
    p.add_argument("--deliver", default="", help="cron failure notice target (default: discord if configured, else local)")
    p.add_argument("--interval", default="10m")
    p.add_argument("--adopt", action="store_true", help="the label already has data and it is this machine's")
    p.add_argument("--skip-initial", action="store_true", help="do not save past sessions now")
    args = p.parse_args()

    label = args.label.strip().lower()
    if not LABEL_RE.match(label):
        die("label must match [a-z0-9][a-z0-9-]{0,31}")

    step(1, "API key")
    current = read_env(ENV_FILE)
    extra: dict[str, str] = {}
    for cand in ([args.env_file] if args.env_file else []) + LEDGER_ENV:
        if cand and cand.is_file():
            extra = read_env(cand)
            break
    key = os.environ.get("KS_API_KEY") or current.get("KS_API_KEY") or extra.get("KS_API_KEY", "")
    if not key:
        die("KS_API_KEY not found. Pass --env-file with a file that holds it; "
            f"looked in {ENV_FILE} and {', '.join(map(str, LEDGER_ENV))}")
    base = (args.api_url or current.get("KS_API_URL") or extra.get("KS_API_URL") or "").rstrip("/")
    if not base:
        die("API URL unknown. Pass --api-url http://<server>:9470 (or put KS_API_URL in the --env-file)")
    host = urllib.parse.urlsplit(base).hostname or "127.0.0.1"
    mcp_url = args.mcp_url or f"http://{host}:9471/sse"
    print(f"      api={base} mcp={mcp_url} label={label}")

    step(2, f"write {ENV_FILE}")
    write_env(ENV_FILE, {"KS_API_URL": base, "KS_API_KEY": key, "KS_HOST_LABEL": label})

    step(3, "check API and label")
    try:
        stats = api(base, key, "/v1/stats")
    except urllib.error.HTTPError as exc:
        die(f"API rejected the key or failed: HTTP {exc.code}")
    except OSError as exc:
        die(f"API unreachable at {base}: {exc}")
    print(f"      opensearch={stats.get('opensearch')} chunks={stats.get('chunks')}")
    used = api(base, key, "/v1/documents", {"kind": "chat", "agent": "hermes", "tag": label, "limit": 1})
    if used.get("total") and not STATE.is_file() and not args.adopt:
        die(f"label '{label}' already has {used['total']} chat chunks from another install. "
            "Pick another label, or pass --adopt if that data came from this machine.")

    step(4, f"skill -> {SKILL_DST}")
    SKILL_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SKILL_SRC, SKILL_DST)

    step(5, "MCP server knowledge-search")
    base_key = "mcp_servers.knowledge-search"
    for k, v in (("url", mcp_url), ("transport", "sse"), ("timeout", "120"), ("connect_timeout", "60"),
                 ("headers.Authorization", "Bearer ${KS_API_KEY}"), ("enabled", "true")):
        hermes("config", "set", f"{base_key}.{k}", v)
    test = hermes("mcp", "test", "knowledge-search", check=False).strip().splitlines() or ["(no output)"]
    print("      " + test[-1])

    step(6, "save past sessions" + (" (skipped)" if args.skip_initial else ""))
    if not args.skip_initial:
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "ingest_hermes_sessions.py"),
                               "--host", label, "--state", str(STATE)])
        if proc.returncode != 0:
            die("initial save failed; fix the error above and re-run (it resumes)")

    step(7, f"cron job {CRON_NAME}")
    STUB.parent.mkdir(parents=True, exist_ok=True)
    STUB.write_text(
        "# knowledge-search autosave stub. Written by install_client.py; re-run it instead of editing.\n"
        "import runpy, sys\n"
        f"TARGET = {str(ROOT / 'scripts' / 'hermes_autosave.py')!r}\n"
        "try:\n"
        "    runpy.run_path(TARGET, run_name='__main__')\n"
        "except FileNotFoundError:\n"
        "    print('knowledge-search: ' + TARGET + ' が見つかりません。リポジトリを確認してください。')\n",
        encoding="utf-8",
    )
    deliver = args.deliver or ("discord" if read_env(ENV_FILE).get("DISCORD_HOME_CHANNEL") else "local")
    if f"Name:      {CRON_NAME}" in hermes("cron", "list"):
        print("      already registered")
    else:
        hermes("cron", "create", f"every {args.interval}", "knowledge-search autosave",
               "--name", CRON_NAME, "--script", STUB.name, "--no-agent", "--deliver", deliver)
        print(f"      created (every {args.interval}, deliver={deliver})")

    print("\nDone. Restart the gateway (`hermes gateway restart`) or run `/reload-mcp` so the "
          "knowledge-search tools load in running sessions.")


if __name__ == "__main__":
    main()
