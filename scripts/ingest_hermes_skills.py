#!/usr/bin/env python3
"""Save this machine's own Hermes skills to knowledge-search as kind=knowledge.

One document per skill: SKILL.md plus the other *.md files in the skill
directory (references/ etc.), so procedures and pitfalls become searchable.

Which skills (see classify())
  Included: skills the user or the agent wrote, and bundled skills the user edited.
  Excluded: bundled skills still identical to their origin hash
  (.bundled_manifest), Hub installs (.hub/lock.json), stale copies of skills
  Hermes ships or once shipped (by name, from the Hermes source tree and its
  git history), and dot directories.

Several machines, one document
  source_id is skill:<path relative to skills/>, without the machine label, so
  the same skill on every machine is one document. When versions differ,
  the newer file time wins (matches the store rule "newer created_at is
  current"); an older copy never overwrites a newer one.

Incremental
  ~/.hermes/state/knowledge_skills.json keeps the last content hash per skill.
  Unchanged skills cost no API call. A skill that disappears here is deleted
  from the store only if this machine was its last writer.

Stdlib only. Reads KS_API_URL / KS_API_KEY / KS_HOST_LABEL like the other scripts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
SKILLS = HERMES_HOME / "skills"
STATE = HERMES_HOME / "state" / "knowledge_skills.json"
JST = timezone(timedelta(hours=9))
KIND = "knowledge"
MAX_CHARS = 250_000  # get_document returns at most 200 chunks of 1600 chars


def _load_env() -> None:
    for raw in (os.environ.get("KS_ENV"), ROOT / ".env", HERMES_HOME / ".env"):
        if not raw or not Path(raw).is_file():
            continue
        for line in Path(raw).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip().startswith("KS_"):
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _call(method: str, path: str, payload: object | None = None) -> dict | None:
    base = os.environ.get("KS_API_URL", "http://127.0.0.1:9470").rstrip("/")
    key = os.environ.get("KS_API_KEY", "")
    if not key:
        sys.exit("KS_API_KEY is missing")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} {method} {path}: {detail[:300]}") from exc


def doc_id_for(source_id: str) -> str:
    # Same formula as api/app/store.make_doc_id.
    return hashlib.sha256(f"{KIND}:{source_id}".encode("utf-8")).hexdigest()[:20]


def _dir_md5(directory: Path) -> str:
    # Same as hermes tools/skills_sync._dir_hash (without its runtime-cache filter).
    hasher = hashlib.md5()
    for fpath in sorted(directory.rglob("*")):
        if fpath.is_file() and "__pycache__" not in fpath.parts:
            hasher.update(str(fpath.relative_to(directory)).encode("utf-8"))
            hasher.update(fpath.read_bytes())
    return hasher.hexdigest()


def _bundled_manifest() -> dict[str, str]:
    path = SKILLS / ".bundled_manifest"
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").split():
            if ":" in line:
                name, digest = line.split(":", 1)
                out[name] = digest
    return out


def _hub_paths() -> set[str]:
    try:
        data = json.loads((SKILLS / ".hub" / "lock.json").read_text(encoding="utf-8"))
        return {v.get("install_path", "") for v in (data.get("installed") or {}).values()}
    except (OSError, ValueError, AttributeError):
        return set()


def _upstream_names() -> set[str]:
    """Skill names Hermes ships now or ever shipped (renamed/removed bundled skills linger locally)."""
    src = HERMES_HOME / "hermes-agent"
    names: set[str] = set()
    for base in ("skills", "optional-skills"):
        if (src / base).is_dir():
            names.update(p.parent.name for p in (src / base).rglob("SKILL.md"))
    if (src / ".git").exists():
        try:
            head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=30).stdout.strip()
            cache = HERMES_HOME / "state" / "knowledge_skills_upstream.json"
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = {}
            if head and cached.get("head") == head:
                return names | set(cached.get("names", []))
            out = subprocess.run(
                ["git", "-C", str(src), "log", "--all", "--name-only", "--pretty=format:", "--",
                 "skills/*SKILL.md", "optional-skills/*SKILL.md"],
                capture_output=True, text=True, timeout=180,
            ).stdout
            hist = {Path(line).parent.name for line in out.splitlines() if line.endswith("SKILL.md")}
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"head": head, "names": sorted(hist)}), encoding="utf-8")
            names |= hist
        except (OSError, subprocess.SubprocessError):
            pass
    return names


def _provenance() -> set[str]:
    """Skills Hermes recorded as agent-created (.usage.json) or edited (.curator_ledger.jsonl)."""
    marked: set[str] = set()
    try:
        usage = json.loads((SKILLS / ".usage.json").read_text(encoding="utf-8"))
        marked |= {k for k, v in usage.items()
                   if isinstance(v, dict) and (v.get("created_by") == "agent" or v.get("agent_created") is True)}
    except (OSError, ValueError, AttributeError):
        pass
    try:
        for line in (SKILLS / ".curator_ledger.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("action") in ("create", "patch", "edit", "write_file", "remove_file") and rec.get("skill"):
                marked.add(rec["skill"])
    except OSError:
        pass
    return marked


def _skill_dirs() -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(SKILLS, followlinks=True):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
        if "SKILL.md" in filenames:
            found.append(Path(dirpath))
    return found


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    out: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t")):
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def classify() -> tuple[list[Path], dict[str, str]]:
    """Decide in this order:
    1. bundled (.bundled_manifest): include only if edited (hash differs from origin)
    2. recorded as agent-created or edited by Hermes: include
    3. Hub install: exclude
    4. a name Hermes ships or ever shipped: exclude (stale copies of old bundled skills)
    5. anything else: the user's own skill from before provenance tracking: include
    """
    manifest, hub, marked, upstream = _bundled_manifest(), _hub_paths(), _provenance(), _upstream_names()
    keep, skipped = [], {}
    for d in _skill_dirs():
        rel = d.relative_to(SKILLS).as_posix()
        if d.name in manifest:
            if _dir_md5(d) == manifest[d.name]:
                skipped[rel] = "bundled, unchanged"
            else:
                keep.append(d)
        elif d.name in marked:
            keep.append(d)
        elif rel in hub:
            skipped[rel] = "hub install"
        elif d.name in upstream:
            skipped[rel] = "shipped by Hermes (old copy)"
        else:
            keep.append(d)
    return keep, skipped


def build(d: Path, label: str) -> dict:
    rel = d.relative_to(SKILLS).as_posix()
    files = [d / "SKILL.md"] + sorted(
        p for p in d.rglob("*.md")
        if p.name != "SKILL.md" and not any(x.startswith(".") for x in p.relative_to(d).parts)
    )
    main = files[0].read_text(encoding="utf-8", errors="replace")
    parts = [main.strip()]
    for f in files[1:]:
        parts.append(f"## {f.relative_to(d).as_posix()}\n\n" + f.read_text(encoding="utf-8", errors="replace").strip())
    body = "\n\n".join(parts)
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + "\n\n[truncated]"
    fm = _frontmatter(main)
    name = fm.get("name") or d.name
    desc = fm.get("description", "")
    mtime = max(f.stat().st_mtime for f in files)
    source_id = f"skill:{rel}"
    return {
        "kind": KIND,
        "title": f"スキル {name}: {desc}" if desc else f"スキル {name}",
        "body": body,
        "tags": ["skill", "hermes", rel.split("/")[0]],
        "agent": "hermes",
        "source_id": source_id,
        "path": f"skills/{rel}",
        "created_at": datetime.fromtimestamp(mtime, JST).isoformat(timespec="seconds"),
        "metadata": {"host": label, "skill": name, "skill_path": rel, "files": [f.relative_to(d).as_posix() for f in files]},
        "_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "_mtime": mtime,
    }


def _ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def main() -> None:
    p = argparse.ArgumentParser(description="Save this machine's own Hermes skills as knowledge")
    p.add_argument("--dry-run", action="store_true", help="list included/excluded skills and exit")
    p.add_argument("--full", action="store_true", help="ignore the local state and re-check every skill")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()
    _load_env()
    label = os.environ.get("KS_HOST_LABEL", "")
    if not label and not args.dry_run:
        sys.exit("KS_HOST_LABEL is missing")

    keep, skipped = classify()
    if args.dry_run:
        for d in keep:
            print("include", d.relative_to(SKILLS).as_posix())
        for rel, why in sorted(skipped.items()):
            print("exclude", rel, f"({why})")
        print(f"include {len(keep)}, exclude {len(skipped)}")
        return

    try:
        state: dict[str, str] = {} if args.full else json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    sent = same = older = deleted = 0
    current: dict[str, str] = {}
    for d in keep:
        doc = build(d, label)
        rel = doc["metadata"]["skill_path"]
        current[rel] = doc["_hash"]
        if state.get(rel) == doc["_hash"]:
            continue
        stored = _call("GET", f"/v1/documents/{doc_id_for(doc['source_id'])}")
        if stored and stored.get("content_hash") == doc["_hash"]:
            same += 1
            continue
        if stored and _ts(stored.get("created_at")) > doc["_mtime"] + 1:
            older += 1  # another machine has a newer version; keep it
            continue
        payload = {k: v for k, v in doc.items() if not k.startswith("_")}
        _call("POST", "/v1/documents/bulk", {"documents": [payload]})
        sent += 1

    for rel in sorted(set(state) - set(current)):
        sid = f"skill:{rel}"
        stored = _call("GET", f"/v1/documents/{doc_id_for(sid)}")
        if stored and (stored.get("metadata") or {}).get("host") == label:
            _call("DELETE", f"/v1/documents/{doc_id_for(sid)}")
            deleted += 1

    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(STATE)
    if not args.quiet:
        print(json.dumps({"skills": len(keep), "excluded": len(skipped), "sent": sent,
                          "unchanged_in_store": same, "newer_elsewhere": older, "deleted": deleted}))


if __name__ == "__main__":
    main()
