"""MCP tools for local Hermes. Talks to the API on localhost so auth stays in one place.

Do not enable postponed annotations. mcp 1.12 calls issubclass() on parameter
annotations and crashes if they are strings.
"""

import hmac
import json
import os

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

API_URL = os.environ.get("KS_API_URL", "http://127.0.0.1:9470").rstrip("/")
API_KEY = os.environ.get("KS_API_KEY", "")
PORT = int(os.environ.get("KS_MCP_PORT", "9471"))

mcp = FastMCP(
    "knowledge-search",
    host="0.0.0.0",
    port=PORT,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# Tools are async on purpose. mcp 1.12 runs a sync tool directly on the event
# loop, so one slow API call would stall every other call on the SSE session and
# its keep-alive pings; the stdio bridge then drops the stream and the replies
# are lost. Async tools keep the loop free, and the timeout bounds each call.
TOOL_TIMEOUT = float(os.environ.get("KS_TOOL_TIMEOUT", "45"))


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=API_URL,
        timeout=httpx.Timeout(TOOL_TIMEOUT, connect=10.0),
        headers={"Authorization": f"Bearer {API_KEY}"},
    )


def _dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
async def knowledge_search(
    query: str,
    kind: str = "",
    tags: str = "",
    agent: str = "",
    session_id: str = "",
    limit: int = 8,
) -> str:
    """Search the user's own knowledge, activity history, and chat logs.

    Call this before answering from memory when the question is about the user's
    proprietary notes, past activity, agent chat logs, or telemetry.
    kind: knowledge, activity, chat, telemetry, file. Comma-separated for several.
    tags: comma-separated; a hit must have every tag.
    Returns snippets, created_at, and doc_id. created_at is the original message
    time in JST. When hits disagree, treat the newer created_at as current.
    Do not use ingested_at for that. Then call knowledge_get for the full text.
    """
    body: dict = {"query": query, "limit": limit}
    if kind:
        body["kind"] = [k.strip() for k in kind.split(",") if k.strip()]
    if tags:
        body["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if agent:
        body["agent"] = agent
    if session_id:
        body["session_id"] = session_id
    async with _client() as client:
        resp = await client.post("/v1/search", json=body)
        resp.raise_for_status()
        return _dump(resp.json())


@mcp.tool()
async def knowledge_get(doc_id: str) -> str:
    """Read one of the user's stored documents, including every chunk, by doc_id from knowledge_search."""
    async with _client() as client:
        resp = await client.get(f"/v1/documents/{doc_id}")
        resp.raise_for_status()
        return _dump(resp.json())


@mcp.tool()
async def knowledge_put(
    body: str,
    title: str = "",
    kind: str = "knowledge",
    tags: str = "",
    agent: str = "",
    session_id: str = "",
    role: str = "",
    source_id: str = "",
    created_at: str = "",
) -> str:
    """Write one document. Re-posting the same source_id updates it instead of duplicating.

    kind: knowledge (notes), activity (what happened), chat (agent logs), telemetry, file.
    """
    payload = {
        "body": body,
        "title": title or None,
        "kind": kind,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "agent": agent or None,
        "session_id": session_id or None,
        "role": role or None,
        "source_id": source_id or None,
        "created_at": created_at or None,
    }
    async with _client() as client:
        resp = await client.post("/v1/documents", json=payload)
        resp.raise_for_status()
        return _dump(resp.json())


@mcp.tool()
async def knowledge_list(kind: str = "", tag: str = "", limit: int = 20) -> str:
    """List the user's stored knowledge, history, or logs, newest first. Optional kind and tag filters."""
    params = {"limit": limit}
    if kind:
        params["kind"] = kind
    if tag:
        params["tag"] = tag
    async with _client() as client:
        resp = await client.get("/v1/documents", params=params)
        resp.raise_for_status()
        return _dump(resp.json())


@mcp.tool()
async def knowledge_tag(doc_id: str, add: str = "", remove: str = "") -> str:
    """Organize a document by adding or removing tags. Comma-separated."""
    payload = {
        "add": [t.strip() for t in add.split(",") if t.strip()],
        "remove": [t.strip() for t in remove.split(",") if t.strip()],
    }
    async with _client() as client:
        resp = await client.patch(f"/v1/documents/{doc_id}/tags", json=payload)
        resp.raise_for_status()
        return _dump(resp.json())


class BearerAuth:
    """ASGI guard for the SSE endpoint, which is published on the LAN.

    Every request (GET /sse and POST /messages/) must carry the same
    Bearer KS_API_KEY as the HTTP API.
    """

    def __init__(self, app, key: str) -> None:
        self.app = app
        self.expected = f"Bearer {key}".encode("utf-8")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not self.expected or not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def main() -> None:
    if not API_KEY:
        raise SystemExit("KS_API_KEY is required")
    uvicorn.run(BearerAuth(mcp.sse_app(), API_KEY), host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
