#!/usr/bin/env python3
"""Load agent memory notes into knowledge-search as kind=knowledge.

Memory notes are the curated facts agents keep about the user and their work,
so they belong with knowledge rather than chat:
  --source claude: ~/.claude/projects/<project>/memory/*.md (one doc per file; MEMORY.md index skipped)
  --source hermes: ~/.hermes/memories/MEMORY.md and USER.md (one doc per §-separated entry)

Stdlib only. Incremental with --state: only new or changed notes are sent,
and notes that disappeared from disk are deleted from the store.
source_id is <label>:memory:<source>:<key>.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ingest_claude_sessions  # noqa: E402
import ingest_hermes_sessions  # noqa: E402
from ingest_hermes_sessions import _post  # noqa: E402

JST = timezone(timedelta(hours=9))
HERMES_HOME = ingest_hermes_sessions.HERMES_HOME
CLAUDE_PROJECTS = ingest_claude_sessions.CODE_ROOT
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.S)


def _doc_id(source_id: str) -> str:
    # Same rule as api/app/store.py make_doc_id for kind=knowledge.
    return hashlib.sha256(f"knowledge:{source_id}".encode("utf-8")).hexdigest()[:20]


def _delete(doc_id: str) -> None:
    base = os.environ.get("KS_API_URL", "http://127.0.0.1:9470").rstrip("/")
    req = urllib.request.Request(base + f"/v1/documents/{doc_id}", method="DELETE",
                                 headers={"Authorization": f"Bearer {os.environ.get('KS_API_KEY', '')}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise SystemExit(f"HTTP {exc.code} deleting {doc_id}") from exc


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, JST).isoformat()


def _project(slug: str) -> str:
    # "-Users-alice-dev-myapp" -> "myapp"
    return slug.rstrip("-").rsplit("-", 1)[-1].lower() or slug


def claude_notes(host: str) -> list[dict]:
    docs = []
    for path in sorted(CLAUDE_PROJECTS.glob("*/memory/*.md")):
        if path.name == "MEMORY.md":
            continue
        text = path.read_text(encoding="utf-8").strip()
        meta, body = {}, text
        m = FRONTMATTER.match(text)
        if m:
            body = text[m.end():].strip()
            for line in m.group(1).splitlines():
                k, _, v = line.strip().partition(":")
                if v.strip():
                    meta.setdefault(k.strip(), v.strip())
        if not body:
            continue
        slug = path.parent.parent.name
        project = _project(slug)
        name = meta.get("name") or path.stem
        title = f"{meta['description']}（{name}）" if meta.get("description") else name
        created = meta.get("modified")
        try:
            created = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(JST).isoformat()
        except (AttributeError, ValueError):
            created = _iso(path.stat().st_mtime)
        tags = ["memory", host, "claude", project]
        if meta.get("type"):
            tags.append(meta["type"])
        docs.append({
            "kind": "knowledge", "title": title, "body": body, "tags": tags, "agent": "claude",
            "source_id": f"{host}:memory:claude:{slug}/{path.name}", "created_at": created,
            "metadata": {"host": host, "source": "claude-memory", "project": project,
                         "path": str(path), "memory_type": meta.get("type")},
        })
    return docs


def hermes_notes(host: str) -> list[dict]:
    docs = []
    for name in ("USER.md", "MEMORY.md"):
        path = HERMES_HOME / "memories" / name
        if not path.is_file():
            continue
        created = _iso(path.stat().st_mtime)
        for entry in re.split(r"(?m)^§\s*$", path.read_text(encoding="utf-8")):
            entry = entry.strip()
            if not entry:
                continue
            key = hashlib.sha256(entry.encode("utf-8")).hexdigest()[:16]
            first = entry.splitlines()[0]
            title = first if len(first) <= 60 else first[:60] + "…"
            docs.append({
                "kind": "knowledge", "title": title, "body": entry,
                "tags": ["memory", host, "hermes", "user" if name == "USER.md" else "memory"],
                "agent": "hermes", "source_id": f"{host}:memory:hermes:{name}:{key}", "created_at": created,
                "metadata": {"host": host, "source": "hermes-memory", "file": name},
            })
    return docs


def _digest(doc: dict) -> str:
    return hashlib.sha256(json.dumps([doc["title"], doc["body"], doc["tags"]], ensure_ascii=False).encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest agent memory notes into knowledge-search")
    parser.add_argument("--source", choices=("claude", "hermes"), required=True)
    parser.add_argument("--host", default="", help="label used in source_id (default: $KS_HOST_LABEL)")
    parser.add_argument("--state", type=Path, default=None, help="incremental: remember sent notes here")
    parser.add_argument("--quiet", action="store_true", help="print only on failure")
    args = parser.parse_args()
    (ingest_claude_sessions if args.source == "claude" else ingest_hermes_sessions)._load_env()
    host = args.host or os.environ.get("KS_HOST_LABEL", "")
    if not host:
        sys.exit("host label is missing: pass --host or set KS_HOST_LABEL")
    docs = claude_notes(host) if args.source == "claude" else hermes_notes(host)
    sent: dict[str, str] = {}
    if args.state and args.state.is_file():
        try:
            sent = json.loads(args.state.read_text(encoding="utf-8")).get("sent", {})
        except (OSError, json.JSONDecodeError):
            sent = {}
    current = {d["source_id"]: _digest(d) for d in docs}
    todo = [d for d in docs if sent.get(d["source_id"]) != current[d["source_id"]]]
    gone = [sid for sid in sent if sid not in current]
    ingested = chunks = 0
    for i in range(0, len(todo), 40):
        result = _post(todo[i : i + 40])
        ingested += int(result.get("ingested", 0))
        chunks += int(result.get("chunks", 0))
    for sid in gone:
        _delete(_doc_id(sid))
    if args.state:
        args.state.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.state.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sent": current, "updated_at": datetime.now(JST).isoformat()}), encoding="utf-8")
        tmp.replace(args.state)
    if not args.quiet:
        print(json.dumps({"notes": len(docs), "ingested": ingested, "chunks": chunks, "deleted": len(gone)},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
