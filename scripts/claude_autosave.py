#!/usr/bin/env python3
"""launchd body: save new Claude.app session messages and Claude Code memory notes of this Mac to knowledge-search.

Run every 10 minutes by the LaunchAgent that install_claude_app.py creates.
Stdlib only.

- Reads KS_API_URL / KS_API_KEY / KS_HOST_LABEL from ~/.config/knowledge-search/client.env.
- Incremental: file offsets are kept in ~/.config/knowledge-search/claude_autosave.json.
- One line per run in ~/.config/knowledge-search/logs/claude_autosave.log.
  On failure also shows a macOS notification. Always exits 0.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KS_HOME = Path(os.environ.get("KS_HOME") or Path.home() / ".config" / "knowledge-search")
STATE = KS_HOME / "claude_autosave.json"
LOG = KS_HOME / "logs" / "claude_autosave.log"


def main() -> None:
    cmd = [sys.executable, str(ROOT / "scripts" / "ingest_claude_sessions.py"), "--state", str(STATE), "--quiet"]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    memories = [sys.executable, str(ROOT / "scripts" / "ingest_memories.py"), "--source", "claude",
                "--state", str(KS_HOME / "claude_memories.json"), "--quiet"]
    ok, outs = True, []
    for c in (cmd, memories):
        try:
            proc = subprocess.run(c, capture_output=True, text=True, timeout=600)
            ok, out = ok and proc.returncode == 0, (proc.stdout + proc.stderr).strip()
        except Exception as exc:  # timeout, missing python, ...
            ok, out = False, f"{type(exc).__name__}: {exc}"
        if out:
            outs.append(out)
    out = "\n".join(outs)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp} {'ok' if ok else 'FAIL'}\n")
        if not ok and out:
            fh.write(out + "\n")
    if not ok:
        msg = "knowledge-search への Claude セッション保存に失敗しました。詳細: " + str(LOG)
        subprocess.run(["osascript", "-e", f"display notification {msg!r} with title \"knowledge-search\""],
                       capture_output=True)
        print(msg)
        print("\n".join(out.splitlines()[-3:]))


if __name__ == "__main__":
    main()
