#!/usr/bin/env python3
"""Load Claude.app sessions (Code tab / Claude Code CLI and Cowork) into knowledge-search.

Both keep Claude Code-format JSONL transcripts on this Mac:
  code:   ~/.claude/projects/<cwd>/<session>.jsonl
  cowork: ~/Library/Application Support/Claude/local-agent-mode-sessions/
          <account>/<org>/local_<id>/.claude/projects/<cwd>/<session>.jsonl
Plain claude.ai chats live on Anthropic's servers, not on disk, so they are not covered.

Stores user and assistant text. Thinking, tool results and sub-agent
transcripts are omitted; tool calls are kept as names. source_id is
<label>:<line uuid>, so a re-run overwrites.

Incremental (--state): remembers the byte offset of each transcript and sends
only lines appended since. When a session's title changes, the whole file is
re-sent so earlier messages get the new title.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ingest_hermes_sessions import _post  # noqa: E402  same bulk API client

KS_HOME = Path(os.environ.get("KS_HOME") or Path.home() / ".config" / "knowledge-search")
APP = Path.home() / "Library" / "Application Support" / "Claude"
CODE_ROOT = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
COWORK_ROOT = APP / "local-agent-mode-sessions"
JST = timezone(timedelta(hours=9))
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def _load_env() -> None:
    """KS_* from $KS_ENV, then ~/.config/knowledge-search/client.env."""
    for raw in (os.environ.get("KS_ENV"), KS_HOME / "client.env"):
        if not raw or not Path(raw).is_file():
            continue
        for line in Path(raw).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.strip().startswith("KS_"):
                    os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def transcripts() -> list[tuple[str, Path]]:
    found = [("code", p) for p in sorted(CODE_ROOT.glob("*/*.jsonl"))]
    found += [("cowork", p) for p in sorted(COWORK_ROOT.glob("*/*/local_*/.claude/projects/*/*.jsonl"))]
    return found


def _cowork_meta(path: Path) -> dict:
    # .../local_<id>/.claude/projects/<cwd>/<session>.jsonl -> .../local_<id>.json
    session_dir = path.parents[3]
    try:
        return json.loads(session_dir.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _title(path: Path, source: str) -> str:
    custom = ai = ""
    with path.open("rb") as fh:
        for raw in fh:
            if b'-title"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "custom-title":
                custom = d.get("customTitle") or custom
            elif d.get("type") == "ai-title":
                ai = d.get("aiTitle") or ai
    if source == "cowork":
        return custom or _cowork_meta(path).get("title") or ai or path.stem
    return custom or ai or path.stem


def _text(content) -> tuple[str, list[str]]:
    if isinstance(content, str):
        return content, []
    parts, tools = [], []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and item.get("text"):
            parts.append(str(item["text"]))
        elif item.get("type") == "tool_use" and item.get("name"):
            tools.append(str(item["name"]))
    return "\n".join(parts), tools


def _iso(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(JST).isoformat()
    except ValueError:
        return None


def documents(lines: list[bytes], host: str, source: str, title: str, path: Path) -> list[dict]:
    docs = []
    for raw in lines:
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if d.get("type") not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
            continue
        msg = d.get("message") or {}
        role = msg.get("role") or d["type"]
        body, tools = _text(msg.get("content"))
        body = REMINDER.sub("", body).strip()
        if body.startswith("<local-command-") or body.startswith("<command-"):
            continue
        if tools:
            body = (body + "\n\n" if body else "") + "tools: " + ", ".join(tools)
        if not body or not d.get("uuid"):
            continue
        tags = ["claude", host, source]
        docs.append(
            {
                "kind": "chat",
                "title": title,
                "body": f"{role}: {body}",
                "tags": tags,
                "agent": "claude",
                "session_id": d.get("sessionId") or path.stem,
                "role": role,
                "source_id": f"{host}:{d['uuid']}",
                "created_at": _iso(d.get("timestamp")),
                "metadata": {
                    "host": host,
                    "uuid": d["uuid"],
                    "source": source,
                    "model": msg.get("model"),
                    "cwd": d.get("cwd"),
                },
            }
        )
    return docs


def _read_state(path: Path | None) -> dict:
    if not path:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("files", {})
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_state(path: Path, files: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"files": files, "updated_at": datetime.now(JST).isoformat()},
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Claude.app sessions into knowledge-search")
    parser.add_argument("--host", default="", help="label used in source_id (default: $KS_HOST_LABEL)")
    parser.add_argument("--batch", type=int, default=40)
    parser.add_argument("--state", type=Path, default=None, help="incremental: remember file offsets here")
    parser.add_argument("--quiet", action="store_true", help="print only on failure")
    args = parser.parse_args()
    _load_env()
    host = args.host or os.environ.get("KS_HOST_LABEL", "")
    if not host:
        sys.exit("host label is missing: pass --host or set KS_HOST_LABEL")
    state = _read_state(args.state)
    ingested = chunks = files_sent = 0
    for source, path in transcripts():
        key = str(path)
        prev = state.get(key, {})
        size = path.stat().st_size
        offset = int(prev.get("offset", 0))
        if size == offset:
            continue
        title = _title(path, source)
        if size < offset or title != prev.get("title"):
            offset = 0
        with path.open("rb") as fh:
            fh.seek(offset)
            blob = fh.read()
        end = blob.rfind(b"\n") + 1  # leave a half-written last line for the next run
        docs = documents(blob[:end].splitlines(), host, source, title, path)
        for i in range(0, len(docs), args.batch):
            result = _post(docs[i : i + args.batch])
            ingested += int(result.get("ingested", 0))
            chunks += int(result.get("chunks", 0))
        files_sent += 1 if docs else 0
        state[key] = {"offset": offset + end, "title": title}
        if args.state:
            _write_state(args.state, state)
        if not args.quiet and docs:
            print(f"{source} {path.name} messages={len(docs)} chunks={chunks}", flush=True)
    if not args.quiet:
        print(json.dumps({"ingested": ingested, "chunks": chunks, "files": files_sent}, ensure_ascii=False))


if __name__ == "__main__":
    main()
