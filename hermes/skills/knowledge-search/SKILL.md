---
name: knowledge-search
description: "Use when looking up personal knowledge, history, or logs."
version: 2.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [OpenSearch, knowledge, Japanese]
---

# knowledge-search

The user's personal knowledge, activity history, Hermes chat logs, and telemetry live in one OpenSearch store on a LAN server. Every Hermes machine searches it over MCP and saves its own sessions into it.

Source of truth for this skill: `hermes/skills/knowledge-search/SKILL.md` in this repository (branch `main`). `scripts/install_client.py` copies it here; edit the repo, not this copy.

## When to Use

Use when the user's own knowledge, history, past decisions, or past chats are needed. Search first, then read the hit:

1. `mcp__knowledge_search__knowledge_search` (Japanese works; kuromoji + n-gram keyword search, so try several wordings and synonyms).
2. `mcp__knowledge_search__knowledge_get` with the `doc_id` for the full text.

Other tools: `knowledge_list`, `knowledge_put` (save a note; set `kind`, `title`, `tags`, and `source_id` to make it overwritable), `knowledge_tag`.

Curated notes are `kind=knowledge`: every machine's autosave also stores agent memory notes (Hermes `~/.hermes/memories/`, Claude Code `memory/*.md`) with tag `memory`. To look up the user's facts and preferences first, search with `kind=knowledge`.

Each hit has `created_at` in JST, the original time, also prefixed on the text. When facts conflict, the newer `created_at` is the current one. Do not use `ingested_at` for that. Chats from every machine are mixed; the machine label is in `tags` and `metadata.host`, and `source_id` is `<label>:<message id>`.

The user's own Hermes skills are also stored as `kind=knowledge` with tag `skill` and `source_id` `skill:<path>` (one document per skill, SKILL.md plus its other .md files). To look only at procedures and pitfalls, search with `kind="knowledge"`; the loaded skill on this machine stays the authority for how to act, the store is for finding which skill or past decision applies.

## Client setup on a machine

```bash
git clone https://github.com/<owner>/knowledge-search.git ~/knowledge-search   # or git pull
python3 ~/knowledge-search/scripts/install_client.py --label host-a --api-url http://<server>:9470 --env-file <file with KS_API_KEY>
hermes gateway restart     # or /reload-mcp in an open session
```

The installer is idempotent. It writes `KS_API_URL`, `KS_API_KEY`, `KS_HOST_LABEL` into `~/.hermes/.env`, registers MCP `knowledge-search` (`http://<server>:9471/sse`, header `Authorization: Bearer ${KS_API_KEY}`), saves all past sessions once, and creates cron `knowledge_autosave` (no_agent, every 10m) that runs `~/.hermes/scripts/knowledge_autosave.py` → `scripts/hermes_autosave.py`. Each run saves new sessions, then the user's own skills (`scripts/ingest_hermes_skills.py`; `--dry-run` lists what is in and why the rest is out). Log: `~/.hermes/logs/knowledge_autosave.log`; state: `~/.hermes/state/knowledge_autosave.json`, `knowledge_skills.json`.

- The API key is not in GitHub. The installer reads it from `~/.hermes/.env`, `--env-file`, the repo `.env` on the server, or `~/.config/knowledge-search/client.env`. Never print it or paste it in chat.
- Labels must be unique per machine. The installer refuses a label that already has chats unless `--adopt` (only when that data really came from this machine). Reusing another machine's label overwrites its messages.
- After `git pull` in the repo, the cron picks up the new code on its next run; re-run the installer only if it changed.
- If saving fails, cron sends one line; check the log. Common causes: server down, LAN unreachable (WSL: outbound is fine, no portproxy needed), key changed.

## Server only

- In the checkout, `docker compose ps`. API 9470 and MCP SSE 9471 are on the LAN and both need the Bearer key. OpenSearch 9200 is 127.0.0.1 only; agents never query it directly. Security plugin off; the key is the lock.
- CLI `ks` (`scripts/ks`, stdlib): `ks search '…'`, `ks ingest ./notes --kind knowledge --tag x`, `echo text | ks put --kind telemetry --title … --source-id …`, `ks get`, `ks tag`, `ks delete`, `ks stats`. Same `source_id` overwrites. Kinds: knowledge, activity, chat, telemetry, file.
- Sized for a host under 4GB RAM: OpenSearch 3.9.0, heap 512MB, container limit 1400MB. Do not add Dashboards, k-NN, ML, or a second node. If OOM, move the compose project to a VPS the user offers.
- `vm.max_map_count=262144` must be persisted on the host (e.g. `/etc/sysctl.d/99-opensearch.conf`). Hermes sudo has no TTY; the user runs sudo in a visible terminal.
- From Hermes, run `docker compose up -d` with `background=true` and health-check in a separate call. First boot takes minutes; wait for `/health`.
- `api/app/mcp_server.py` must not use `from __future__ import annotations` (mcp 1.12 crashes on string annotations). A tool-description change needs `docker compose build api && docker compose up -d api`, then a Hermes MCP reload on every machine.
- Analyzer changes need a new index and a re-ingest. A major OpenSearch jump (2.x → 3.x) needs a new volume.

## Pitfalls

- Do not also wire `hooks.outbound` `post_llm_call` to the store: the payload has no message id, so every turn would be stored twice, and failed deliveries are dropped.
- MCP tool names use double underscores: `mcp__knowledge_search__knowledge_search`.
