"""HTTP API for ingest, search, and read."""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app import store

API_KEY = os.environ.get("KS_API_KEY", "")
MAX_BYTES = 25 * 1024 * 1024
TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".log",
    ".json",
    ".csv",
    ".yaml",
    ".yml",
    ".rst",
    ".org",
    ".tex",
}

app = FastAPI(title="knowledge-search", version="0.1.0")


def require_key(authorization: str | None = Header(default=None)) -> None:
    if not API_KEY:
        raise HTTPException(500, "KS_API_KEY is not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(token, API_KEY):
        raise HTTPException(403, "invalid token")


class DocumentIn(BaseModel):
    kind: str = "knowledge"
    title: str | None = None
    body: str
    tags: list[str] = Field(default_factory=list)
    lang: str = "ja"
    agent: str | None = None
    session_id: str | None = None
    role: str | None = None
    path: str | None = None
    source_id: str | None = None
    doc_id: str | None = None
    created_at: str | None = None
    metadata: dict[str, Any] | None = None


class BulkIn(BaseModel):
    documents: list[DocumentIn]


class SearchIn(BaseModel):
    query: str
    kind: str | list[str] | None = None
    tags: list[str] = Field(default_factory=list)
    agent: str | None = None
    session_id: str | None = None
    role: str | None = None
    lang: str | None = None
    path_prefix: str | None = None
    since: str | None = None
    until: str | None = None
    limit: int = 8
    offset: int = 0


class TagUpdate(BaseModel):
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


@app.on_event("startup")
async def startup() -> None:
    await store.wait_ready()
    await store.ensure_index()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _raise(exc: store.StoreError) -> None:
    raise HTTPException(exc.status, exc.detail) from exc


@app.post("/v1/documents", dependencies=[Depends(require_key)])
async def put_document(doc: DocumentIn) -> dict[str, Any]:
    try:
        return await store.upsert_many([doc.model_dump()])
    except store.StoreError as exc:
        _raise(exc)


@app.post("/v1/documents/bulk", dependencies=[Depends(require_key)])
async def put_bulk(body: BulkIn) -> dict[str, Any]:
    try:
        return await store.upsert_many([doc.model_dump() for doc in body.documents])
    except store.StoreError as exc:
        _raise(exc)


@app.post("/v1/files", dependencies=[Depends(require_key)])
async def upload_files(
    files: list[UploadFile] = File(...),
    kind: str = Form("file"),
    tags: str = Form(""),
    agent: str = Form(""),
) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    for upload in files:
        raw = await upload.read()
        if len(raw) > MAX_BYTES:
            raise HTTPException(413, f"{upload.filename} exceeds 25MB")
        name = upload.filename or "upload.txt"
        suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if suffix and suffix not in TEXT_SUFFIXES:
            raise HTTPException(415, f"{name} is not a text file")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        documents.append(
            {
                "kind": kind,
                "title": name,
                "body": text,
                "path": name,
                "source_id": name,
                "tags": tag_list,
                "agent": agent or None,
            }
        )
    try:
        return await store.upsert_many(documents)
    except store.StoreError as exc:
        _raise(exc)


@app.post("/v1/search", dependencies=[Depends(require_key)])
async def search(body: SearchIn) -> dict[str, Any]:
    try:
        return await store.search(body.model_dump())
    except store.StoreError as exc:
        _raise(exc)


@app.get("/v1/documents/{doc_id}", dependencies=[Depends(require_key)])
async def get_document(doc_id: str) -> dict[str, Any]:
    try:
        return await store.get_document(doc_id)
    except store.StoreError as exc:
        _raise(exc)


@app.get("/v1/documents", dependencies=[Depends(require_key)])
async def list_documents(
    kind: str | None = None,
    tag: str | None = None,
    agent: str | None = None,
    session_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    query: dict[str, Any] = {"limit": limit, "offset": offset}
    if kind:
        query["kind"] = kind
    if tag:
        query["tags"] = [tag]
    if agent:
        query["agent"] = agent
    if session_id:
        query["session_id"] = session_id
    try:
        return await store.list_documents(query)
    except store.StoreError as exc:
        _raise(exc)


@app.patch("/v1/documents/{doc_id}/tags", dependencies=[Depends(require_key)])
async def patch_tags(doc_id: str, body: TagUpdate) -> dict[str, Any]:
    try:
        return await store.update_tags(doc_id, body.add, body.remove)
    except store.StoreError as exc:
        _raise(exc)


@app.delete("/v1/documents/{doc_id}", dependencies=[Depends(require_key)])
async def delete_document(doc_id: str) -> dict[str, Any]:
    try:
        return await store.delete_document(doc_id)
    except store.StoreError as exc:
        _raise(exc)


@app.get("/v1/stats", dependencies=[Depends(require_key)])
async def stats() -> dict[str, Any]:
    try:
        return await store.stats()
    except store.StoreError as exc:
        _raise(exc)
