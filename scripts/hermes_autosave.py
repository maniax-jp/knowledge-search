#!/usr/bin/env python3
"""Cron body: save this machine's Hermes sessions, memory notes and own skills to knowledge-search.

Run by a Hermes no_agent cron job through the stub that install_client.py puts
in ~/.hermes/scripts/. Stdlib only, works on Linux, WSL and macOS.

Steps (each runs even if another fails):
  sessions  scripts/ingest_hermes_sessions.py  kind=chat, incremental by message id
  memories  scripts/ingest_memories.py         kind=knowledge, MEMORY.md / USER.md entries
  skills    scripts/ingest_hermes_skills.py    kind=knowledge, only changed skills

- Reads KS_API_URL / KS_API_KEY / KS_HOST_LABEL from ~/.hermes/.env.
- Silent on success. On failure prints a short notice (cron delivers it) and
  always exits 0 so the scheduler does not add its own alert on top.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
STATE = HERMES_HOME / "state" / "knowledge_autosave.json"
LOG = HERMES_HOME / "logs" / "knowledge_autosave.log"
# One run never sends more than this many messages; a long backlog is worked
# off over several runs because the state advances after each run.
PER_RUN = 2000

STEPS = [
    ("sessions", ["ingest_hermes_sessions.py", "--state", str(STATE), "--limit", str(PER_RUN), "--quiet"]),
    ("memories", ["ingest_memories.py", "--source", "hermes",
                  "--state", str(HERMES_HOME / "state" / "knowledge_memories.json"), "--quiet"]),
    ("skills", ["ingest_hermes_skills.py", "--quiet"]),
]


def _run(script: str, *args: str) -> tuple[bool, str]:
    cmd = [sys.executable, str(ROOT / "scripts" / script), *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()
    except Exception as exc:  # timeout, missing python, ...
        return False, f"{type(exc).__name__}: {exc}"


def _label() -> str:
    try:
        for line in (HERMES_HOME / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("KS_HOST_LABEL="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "?"


def main() -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    failed = []
    with LOG.open("a", encoding="utf-8") as fh:
        for name, (script, *args) in STEPS:
            ok, out = _run(script, *args)
            fh.write(f"{stamp} {name} {'ok' if ok else 'FAIL'}\n")
            if not ok:
                fh.write(out + "\n")
                failed.append((name, out))
    for name, out in failed:
        print(f"knowledge-search への保存に失敗しました（{_label()}・{name}）。詳細: {LOG}")
        print("\n".join(out.splitlines()[-3:]))


if __name__ == "__main__":
    main()
