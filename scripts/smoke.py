#!/usr/bin/env python3
"""Live checks against a running knowledge-search API. Stdlib only."""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ks = SourceFileLoader("ks", str(ROOT / "scripts" / "ks")).load_module()


def main() -> None:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: object) -> None:
        if cond:
            print(f"PASS {name}")
        else:
            failures.append(name)
            print(f"FAIL {name}: {detail}")

    put = ks._request(
        "POST",
        "/v1/documents",
        {
            "kind": "knowledge",
            "title": "形態素検証",
            "body": "彼は毎朝公園を走りました。受入検査では納品書と現品を照合する。",
            "source_id": "smoke-morph",
            "tags": ["smoke"],
            "agent": "smoke",
        },
    )
    assert isinstance(put, dict)
    doc_id = put["doc_ids"][0]
    check("put", bool(doc_id), put)

    ran = ks._request("POST", "/v1/search", {"query": "走る", "tags": ["smoke"], "limit": 5})
    assert isinstance(ran, dict)
    check("kuromoji-baseform", any(h["doc_id"] == doc_id for h in ran["hits"]), ran)

    noun = ks._request("POST", "/v1/search", {"query": "納品書", "tags": ["smoke"], "limit": 5})
    assert isinstance(noun, dict)
    check("kuromoji-noun", any(h["doc_id"] == doc_id for h in noun["hits"]), noun)

    ngram_doc = ks._request(
        "POST",
        "/v1/documents",
        {
            "kind": "knowledge",
            "title": "固有句",
            "body": "黒文字トークナイザ検証用固有句：月光の書架。この句は辞書に無い。",
            "source_id": "smoke-ngram",
            "tags": ["smoke"],
        },
    )
    assert isinstance(ngram_doc, dict)
    ngram_id = ngram_doc["doc_ids"][0]
    ngram = ks._request("POST", "/v1/search", {"query": "月光の書架", "tags": ["smoke"], "limit": 5})
    assert isinstance(ngram, dict)
    check("ngram", any(h["doc_id"] == ngram_id for h in ngram["hits"]), ngram)

    sample = ROOT / ".smoke-sample.txt"
    sample.write_text("活動履歴のサンプル。2026-10-15 に knowledge-search をサーバーで起動した。\n", encoding="utf-8")
    try:
        ingested = ks._request(
            "POST",
            "/v1/documents/bulk",
            {
                "documents": [
                    {
                        "kind": "activity",
                        "title": "起動記録",
                        "body": sample.read_text(encoding="utf-8"),
                        "path": ".smoke-sample.txt",
                        "source_id": "smoke-activity",
                        "tags": ["smoke"],
                    },
                    {
                        "kind": "chat",
                        "title": "試験会話",
                        "body": "ユーザー: 在庫回転率を見直したい。\nassistant: 期間在庫を売上原価で割る。",
                        "source_id": "smoke-chat",
                        "session_id": "smoke-session",
                        "role": "assistant",
                        "agent": "smoke",
                        "tags": ["smoke"],
                    },
                    {
                        "kind": "telemetry",
                        "title": "cron ok",
                        "body": "telemetry writer ping. exit 0.",
                        "source_id": "smoke-telemetry",
                        "agent": "cron",
                        "tags": ["smoke", "cron"],
                    },
                ]
            },
        )
    finally:
        sample.unlink(missing_ok=True)
    assert isinstance(ingested, dict)
    check("bulk", ingested.get("ingested") == 3, ingested)

    chat = ks._request("POST", "/v1/search", {"query": "在庫回転率", "kind": "chat", "limit": 5})
    assert isinstance(chat, dict)
    check("chat-search", chat["total"] >= 1, chat)
    if chat["hits"]:
        got = ks._request("GET", f"/v1/documents/{chat['hits'][0]['doc_id']}")
        assert isinstance(got, dict)
        check("get", "在庫回転率" in got.get("text", ""), got)

    tagged = ks._request("PATCH", f"/v1/documents/{doc_id}/tags", {"add": ["reviewed"], "remove": []})
    assert isinstance(tagged, dict)
    check("tag", "reviewed" in tagged.get("tags", []), tagged)

    stats = ks._request("GET", "/v1/stats")
    assert isinstance(stats, dict)
    check("stats", stats.get("opensearch") in ("green", "yellow"), stats)

    # idempotent rewrite
    again = ks._request(
        "POST",
        "/v1/documents",
        {
            "kind": "telemetry",
            "title": "cron ok",
            "body": "telemetry writer ping. exit 0. updated.",
            "source_id": "smoke-telemetry",
            "agent": "cron",
            "tags": ["smoke"],
        },
    )
    assert isinstance(again, dict)
    listed = ks._request("GET", "/v1/documents", query={"kind": "telemetry", "tag": "smoke", "limit": 20})
    assert isinstance(listed, dict)
    ids = [d["doc_id"] for d in listed["documents"]]
    check("idempotent", ids.count(again["doc_ids"][0]) == 1, listed)

    if failures:
        print("FAILED", ", ".join(failures))
        sys.exit(1)
    print("SMOKE_OK")


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
