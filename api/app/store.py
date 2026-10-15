"""OpenSearch index setup and document IO."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from app.chunking import chunk_text

INDEX = os.environ.get("KS_INDEX", "knowledge")
OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://opensearch:9200").rstrip("/")
KINDS = ("knowledge", "activity", "chat", "telemetry", "file")

INDEX_BODY: dict[str, Any] = {
    "settings": {
        "index": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "refresh_interval": "1s",
            "max_ngram_diff": 1,
        },
        "analysis": {
            "tokenizer": {
                "ja_ngram_tokenizer": {"type": "ngram", "min_gram": 2, "max_gram": 3},
                "kuromoji_search": {
                    "type": "kuromoji_tokenizer",
                    "mode": "search",
                    "discard_punctuation": True,
                },
            },
            "analyzer": {
                "ja_kuromoji": {
                    "type": "custom",
                    "tokenizer": "kuromoji_search",
                    "filter": [
                        "kuromoji_baseform",
                        "kuromoji_part_of_speech",
                        "cjk_width",
                        "lowercase",
                        "kuromoji_stemmer",
                    ],
                },
                "ja_ngram": {
                    "type": "custom",
                    "tokenizer": "ja_ngram_tokenizer",
                    "filter": ["cjk_width", "lowercase"],
                },
            },
        },
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "doc_id": {"type": "keyword"},
            "chunk_index": {"type": "integer"},
            "chunk_count": {"type": "integer"},
            "kind": {"type": "keyword"},
            "title": {
                "type": "text",
                "analyzer": "ja_kuromoji",
                "fields": {
                    "ngram": {"type": "text", "analyzer": "ja_ngram"},
                    "keyword": {"type": "keyword", "ignore_above": 512},
                },
            },
            "body": {
                "type": "text",
                "analyzer": "ja_kuromoji",
                "fields": {"ngram": {"type": "text", "analyzer": "ja_ngram"}},
            },
            "tags": {"type": "keyword"},
            "lang": {"type": "keyword"},
            "agent": {"type": "keyword"},
            "session_id": {"type": "keyword"},
            "role": {"type": "keyword"},
            "path": {"type": "keyword"},
            "source_id": {"type": "keyword"},
            "created_at": {"type": "date"},
            "ingested_at": {"type": "date"},
            "content_hash": {"type": "keyword"},
            "metadata_json": {"type": "text", "index": False},
        },
    },
}


class StoreError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_doc_id(kind: str, source_id: str | None, path: str | None, body: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    key = source_id or path or hashlib.sha256(body.encode("utf-8")).hexdigest()
    return hashlib.sha256(f"{kind}:{key}".encode("utf-8")).hexdigest()[:20]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=OPENSEARCH_URL, timeout=httpx.Timeout(60.0, connect=10.0))


async def wait_ready(attempts: int = 100, pause: float = 3.0) -> None:
    import asyncio

    last = "not started"
    async with _client() as client:
        for _ in range(attempts):
            try:
                resp = await client.get("/")
                if resp.status_code == 200 and "opensearch" in resp.text.lower():
                    return
                last = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                last = str(exc)
            await asyncio.sleep(pause)
    raise StoreError(503, f"OpenSearch did not become ready: {last}")


async def ensure_index() -> None:
    async with _client() as client:
        resp = await client.get(f"/{INDEX}")
        if resp.status_code == 200:
            return
        if resp.status_code != 404:
            raise StoreError(resp.status_code, resp.text[:500])
        created = await client.put(f"/{INDEX}", json=INDEX_BODY)
        if created.status_code not in (200, 201):
            raise StoreError(created.status_code, created.text[:800])


def _doc_payload(doc: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    body = (doc.get("body") or "").strip()
    if not body:
        raise StoreError(422, "body is required")
    kind = (doc.get("kind") or "knowledge").strip() or "knowledge"
    title = (doc.get("title") or "").strip() or None
    source_id = (doc.get("source_id") or "").strip() or None
    path = (doc.get("path") or "").strip() or None
    explicit = (doc.get("doc_id") or "").strip() or None
    if explicit and not all(c.isalnum() or c in "_.:-" for c in explicit):
        raise StoreError(422, "doc_id may contain only letters, digits, and _ . : -")
    doc_id = make_doc_id(kind, source_id, path, body, explicit)
    tags = doc.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    tags = [str(t) for t in tags if str(t).strip()]
    created_at = doc.get("created_at") or _now()
    metadata = doc.get("metadata")
    metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata is not None else None
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    pieces = chunk_text(body)
    if not pieces:
        raise StoreError(422, "body is empty after normalization")
    if not title:
        title = (path.rsplit("/", 1)[-1] if path else pieces[0][:80])
    ingested_at = _now()
    actions: list[dict[str, Any]] = []
    for i, piece in enumerate(pieces):
        actions.append(
            {
                "_id": f"{doc_id}:{i}",
                "_source": {
                    "doc_id": doc_id,
                    "chunk_index": i,
                    "chunk_count": len(pieces),
                    "kind": kind,
                    "title": title,
                    "body": f"[{created_at}] {piece}",
                    "tags": tags,
                    "lang": doc.get("lang") or "ja",
                    "agent": doc.get("agent") or None,
                    "session_id": doc.get("session_id") or None,
                    "role": doc.get("role") or None,
                    "path": path,
                    "source_id": source_id,
                    "created_at": created_at,
                    "ingested_at": ingested_at,
                    "content_hash": content_hash,
                    "metadata_json": metadata_json,
                },
            }
        )
    return doc_id, actions


async def upsert_many(documents: list[dict[str, Any]]) -> dict[str, Any]:
    if not documents:
        raise StoreError(422, "documents is empty")
    if len(documents) > 200:
        raise StoreError(422, "at most 200 documents per request")
    prepared: list[tuple[str, list[dict[str, Any]]]] = []
    for doc in documents:
        prepared.append(_doc_payload(doc))

    lines: list[str] = []
    for doc_id, actions in prepared:
        for action in actions:
            lines.append(json.dumps({"index": {"_index": INDEX, "_id": action["_id"]}}, ensure_ascii=False))
            lines.append(json.dumps(action["_source"], ensure_ascii=False))
    payload = "\n".join(lines) + "\n"

    async with _client() as client:
        resp = await client.post(
            "/_bulk",
            content=payload.encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
            params={"refresh": "wait_for"},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        if resp.status_code not in (200, 201):
            raise StoreError(resp.status_code, resp.text[:800])
        data = resp.json()
        if data.get("errors"):
            failed = [
                item
                for item in data.get("items", [])
                if item.get("index", {}).get("error")
            ]
            raise StoreError(500, json.dumps(failed[:3], ensure_ascii=False)[:800])
        for doc_id, actions in prepared:
            count = actions[0]["_source"]["chunk_count"]
            deleted = await client.post(
                f"/{INDEX}/_delete_by_query",
                params={"refresh": "true", "conflicts": "proceed"},
                json={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"doc_id": doc_id}},
                                {"range": {"chunk_index": {"gte": count}}},
                            ]
                        }
                    }
                },
            )
            if deleted.status_code not in (200, 201):
                raise StoreError(deleted.status_code, deleted.text[:500])
    return {
        "ingested": len(prepared),
        "chunks": sum(len(actions) for _, actions in prepared),
        "doc_ids": [doc_id for doc_id, _ in prepared],
    }


def _filters(query: dict[str, Any]) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    kinds = query.get("kind") or query.get("kinds")
    if isinstance(kinds, str) and kinds:
        kinds = [kinds]
    if kinds:
        filters.append({"terms": {"kind": list(kinds)}})
    tags = query.get("tags") or []
    if isinstance(tags, str) and tags:
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    for tag in tags:
        filters.append({"term": {"tags": tag}})
    for field in ("agent", "session_id", "role", "doc_id", "lang"):
        value = query.get(field)
        if value:
            filters.append({"term": {field: value}})
    path_prefix = query.get("path_prefix")
    if path_prefix:
        filters.append({"prefix": {"path": path_prefix}})
    since = query.get("since")
    until = query.get("until")
    if since or until:
        span: dict[str, Any] = {}
        if since:
            span["gte"] = since
        if until:
            span["lte"] = until
        filters.append({"range": {"created_at": span}})
    return filters


async def search(query: dict[str, Any]) -> dict[str, Any]:
    text = (query.get("query") or query.get("q") or "").strip()
    if not text:
        raise StoreError(422, "query is required")
    limit = int(query.get("limit") or 8)
    limit = max(1, min(limit, 50))
    offset = max(0, int(query.get("offset") or 0))
    body = {
        "from": offset,
        "size": limit,
        "track_total_hits": True,
        "query": {
            "bool": {
                "must": [
                    {
                        "bool": {
                            "should": [
                                {"match": {"title": {"query": text, "boost": 3}}},
                                {"match": {"body": {"query": text, "boost": 2}}},
                                {"match": {"title.ngram": {"query": text, "boost": 1.2}}},
                                {"match": {"body.ngram": {"query": text, "boost": 1}}},
                            ],
                            "minimum_should_match": 1,
                        }
                    }
                ],
                "filter": _filters(query),
            }
        },
        "highlight": {
            "pre_tags": ["【"],
            "post_tags": ["】"],
            "fields": {
                "body": {"fragment_size": 180, "number_of_fragments": 2},
                "title": {"fragment_size": 80, "number_of_fragments": 1},
            },
        },
        "_source": [
            "doc_id",
            "chunk_index",
            "chunk_count",
            "kind",
            "title",
            "tags",
            "path",
            "agent",
            "session_id",
            "role",
            "created_at",
            "source_id",
        ],
    }
    async with _client() as client:
        resp = await client.post(f"/{INDEX}/_search", json=body)
        if resp.status_code != 200:
            raise StoreError(resp.status_code, resp.text[:800])
        data = resp.json()
    hits = []
    for hit in data.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        highlight = hit.get("highlight") or {}
        snippet = " ".join(highlight.get("body") or highlight.get("title") or [])
        hits.append(
            {
                "doc_id": src.get("doc_id"),
                "chunk_index": src.get("chunk_index"),
                "chunk_count": src.get("chunk_count"),
                "kind": src.get("kind"),
                "title": src.get("title"),
                "snippet": snippet,
                "score": hit.get("_score"),
                "tags": src.get("tags") or [],
                "path": src.get("path"),
                "agent": src.get("agent"),
                "session_id": src.get("session_id"),
                "role": src.get("role"),
                "created_at": src.get("created_at"),
                "source_id": src.get("source_id"),
            }
        )
    total = data.get("hits", {}).get("total", {}).get("value", 0)
    return {"hits": hits, "total": total, "limit": limit, "offset": offset}


async def get_document(doc_id: str) -> dict[str, Any]:
    body = {
        "size": 200,
        "query": {"term": {"doc_id": doc_id}},
        "sort": [{"chunk_index": "asc"}],
    }
    async with _client() as client:
        resp = await client.post(f"/{INDEX}/_search", json=body)
        if resp.status_code != 200:
            raise StoreError(resp.status_code, resp.text[:800])
        hits = resp.json().get("hits", {}).get("hits", [])
    if not hits:
        raise StoreError(404, "document not found")
    sources = [hit["_source"] for hit in hits]
    first = sources[0]
    metadata = None
    if first.get("metadata_json"):
        try:
            metadata = json.loads(first["metadata_json"])
        except json.JSONDecodeError:
            metadata = first["metadata_json"]
    return {
        "doc_id": doc_id,
        "kind": first.get("kind"),
        "title": first.get("title"),
        "tags": first.get("tags") or [],
        "lang": first.get("lang"),
        "agent": first.get("agent"),
        "session_id": first.get("session_id"),
        "role": first.get("role"),
        "path": first.get("path"),
        "source_id": first.get("source_id"),
        "created_at": first.get("created_at"),
        "ingested_at": first.get("ingested_at"),
        "content_hash": first.get("content_hash"),
        "chunk_count": first.get("chunk_count"),
        "metadata": metadata,
        "chunks": [
            {"chunk_index": src.get("chunk_index"), "body": src.get("body")}
            for src in sources
        ],
        "text": "\n\n".join(src.get("body") or "" for src in sources),
    }


async def list_documents(query: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(query.get("limit") or 20), 100))
    offset = max(0, int(query.get("offset") or 0))
    body = {
        "from": offset,
        "size": limit,
        "track_total_hits": True,
        "query": {"bool": {"filter": _filters(query) or [{"match_all": {}}]}},
        "collapse": {"field": "doc_id"},
        "sort": [{"ingested_at": "desc"}],
        "_source": [
            "doc_id",
            "kind",
            "title",
            "tags",
            "path",
            "agent",
            "session_id",
            "created_at",
            "ingested_at",
            "chunk_count",
            "source_id",
        ],
    }
    async with _client() as client:
        resp = await client.post(f"/{INDEX}/_search", json=body)
        if resp.status_code != 200:
            raise StoreError(resp.status_code, resp.text[:800])
        data = resp.json()
    docs = [hit.get("_source", {}) for hit in data.get("hits", {}).get("hits", [])]
    return {
        "documents": docs,
        "total": data.get("hits", {}).get("total", {}).get("value", 0),
        "limit": limit,
        "offset": offset,
    }


async def update_tags(doc_id: str, add: list[str], remove: list[str]) -> dict[str, Any]:
    current = await get_document(doc_id)
    tags = set(current.get("tags") or [])
    tags.update(t for t in add if t)
    tags.difference_update(t for t in remove if t)
    new_tags = sorted(tags)
    async with _client() as client:
        resp = await client.post(
            f"/{INDEX}/_update_by_query",
            params={"refresh": "true", "conflicts": "proceed"},
            json={
                "query": {"term": {"doc_id": doc_id}},
                "script": {
                    "source": "ctx._source.tags = params.tags",
                    "params": {"tags": new_tags},
                },
            },
        )
        if resp.status_code != 200:
            raise StoreError(resp.status_code, resp.text[:800])
    return {"doc_id": doc_id, "tags": new_tags}


async def delete_document(doc_id: str) -> dict[str, Any]:
    async with _client() as client:
        resp = await client.post(
            f"/{INDEX}/_delete_by_query",
            params={"refresh": "true", "conflicts": "proceed"},
            json={"query": {"term": {"doc_id": doc_id}}},
        )
        if resp.status_code != 200:
            raise StoreError(resp.status_code, resp.text[:500])
        deleted = resp.json().get("deleted", 0)
    if not deleted:
        raise StoreError(404, "document not found")
    return {"doc_id": doc_id, "deleted_chunks": deleted}


async def stats() -> dict[str, Any]:
    body = {
        "size": 0,
        "aggs": {"by_kind": {"terms": {"field": "kind", "size": 20}}},
    }
    async with _client() as client:
        health = await client.get("/_cluster/health")
        resp = await client.post(f"/{INDEX}/_search", json=body)
        count = await client.get(f"/{INDEX}/_count")
    if resp.status_code != 200:
        raise StoreError(resp.status_code, resp.text[:500])
    buckets = resp.json().get("aggregations", {}).get("by_kind", {}).get("buckets", [])
    return {
        "opensearch": health.json().get("status") if health.status_code == 200 else "unknown",
        "chunks": count.json().get("count") if count.status_code == 200 else None,
        "by_kind": {b["key"]: b["doc_count"] for b in buckets},
    }
