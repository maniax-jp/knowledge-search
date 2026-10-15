#!/usr/bin/env python3
"""Load Hermes session messages from state.db into knowledge-search.

Stores user and assistant text, plus compression summaries. Tool payloads
are omitted. source_id is <label>:<message id>, so a re-run overwrites.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
DEFAULT_DB = HERMES_HOME / "state.db"


def _load_env() -> None:
    """KS_* from $KS_ENV, the repo .env (server host), then ~/.hermes/.env (clients)."""
    paths = [os.environ.get("KS_ENV"), ROOT / ".env", HERMES_HOME / ".env"]
    for raw in paths:
        if not raw or not Path(raw).is_file():
            continue
        for line in Path(raw).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip().startswith("KS_"):
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _post(documents: list[dict]) -> dict:
    base = os.environ.get("KS_API_URL", "http://127.0.0.1:9470").rstrip("/")
    key = os.environ.get("KS_API_KEY", "")
    if not key:
        sys.exit("KS_API_KEY is missing")
    req = urllib.request.Request(
        base + "/v1/documents/bulk",
        data=json.dumps({"documents": documents}, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} {detail[:500]}") from exc


def _text(content: str | None) -> str:
    if not content:
        return ""
    text = content.strip()
    if text.startswith("[") or text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, list):
            parts = []
            for item in parsed:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and item.get("text"):
                    parts.append(str(item["text"]))
            return "\n".join(parts).strip()
    return text


def _tool_names(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        calls = json.loads(raw)
    except json.JSONDecodeError:
        return []
    names = []
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            name = fn.get("name") or call.get("name")
            if name:
                names.append(str(name))
    return names


JST = timezone(timedelta(hours=9))


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), JST).isoformat()


def rows(db: Path, host: str, since_id: int = 0) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    query = """
    SELECT m.id, m.session_id, m.role, m.content, m.tool_calls, m.timestamp,
           m.active, m.compacted, s.title, s.source, s.model
    FROM messages m
    LEFT JOIN sessions s ON s.id = m.session_id
    WHERE m.role IN ('user', 'assistant')
      AND m.content IS NOT NULL
      AND length(trim(m.content)) > 0
      AND (m.active = 1 OR m.compacted = 1)
      AND m.id > ?
    ORDER BY m.id
    """
    documents = []
    for row in con.execute(query, (since_id,)):
        body = _text(row["content"])
        names = _tool_names(row["tool_calls"])
        if names:
            body = (body + "\n\n" if body else "") + "tools: " + ", ".join(names)
        if not body.strip():
            continue
        source = row["source"] or "unknown"
        title = (row["title"] or "").strip() or row["session_id"]
        tags = ["hermes", host, source]
        if row["compacted"]:
            tags.append("compacted")
        documents.append(
            {
                "kind": "chat",
                "title": title,
                "body": f"{row['role']}: {body}",
                "tags": tags,
                "agent": "hermes",
                "session_id": row["session_id"],
                "role": row["role"],
                "source_id": f"{host}:{row['id']}",
                "created_at": _iso(row["timestamp"]),
                "metadata": {
                    "host": host,
                    "message_id": row["id"],
                    "source": source,
                    "model": row["model"],
                    "compacted": bool(row["compacted"]),
                },
            }
        )
    return documents


def _read_state(path: Path) -> int:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("last_message_id", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def _write_state(path: Path, last_id: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_message_id": last_id, "updated_at": datetime.now(JST).isoformat()}), encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Hermes sessions into knowledge-search")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--host", default="", help="host label used in source_id (default: $KS_HOST_LABEL)")
    parser.add_argument("--batch", type=int, default=40)
    parser.add_argument("--limit", type=int, default=0, help="ingest only the first N messages")
    parser.add_argument("--state", type=Path, default=None, help="incremental: remember the last message id here")
    parser.add_argument("--overlap", type=int, default=300, help="incremental: re-send this many recent ids (title updates)")
    parser.add_argument("--quiet", action="store_true", help="print only on failure")
    args = parser.parse_args()
    _load_env()
    args.host = args.host or os.environ.get("KS_HOST_LABEL", "")
    if not args.host:
        sys.exit("host label is missing: pass --host or set KS_HOST_LABEL")
    since_id = 0
    if args.state:
        since_id = max(0, _read_state(args.state) - args.overlap)
    docs = rows(args.db, args.host, since_id)
    if args.limit:
        docs = docs[: args.limit]
    if not args.quiet:
        print(f"messages {len(docs)} since_id={since_id}", flush=True)
    ingested = 0
    chunks = 0
    for i in range(0, len(docs), args.batch):
        batch = docs[i : i + args.batch]
        result = _post(batch)
        ingested += int(result.get("ingested", 0))
        chunks += int(result.get("chunks", 0))
        if not args.quiet:
            print(f"progress {i + len(batch)}/{len(docs)} chunks={chunks}", flush=True)
    if args.state and docs:
        last = max(int(d["metadata"]["message_id"]) for d in docs)
        _write_state(args.state, max(last, _read_state(args.state)))
    if not args.quiet:
        print(json.dumps({"ingested": ingested, "chunks": chunks}, ensure_ascii=False))


if __name__ == "__main__":
    main()
