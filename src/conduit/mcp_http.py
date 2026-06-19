"""HTTP (streamable-HTTP) transport for the Conduit MCP server.

The stdio transport (`conduit-mcp`) is for local clients that spawn Conduit as a
subprocess (Claude Desktop, Cursor, Windsurf, VS Code). This module serves the SAME
`server` and the same tools over streamable-HTTP, so REMOTE MCP clients that connect
by URL (ChatGPT connectors, hosted setups) can use Conduit too. The MCP endpoint is
mounted at ``/mcp``.

Security: this endpoint is network-exposed and the tools move a Lightning wallet, so
every request must carry the Conduit API key (``Authorization: Bearer <key>`` or
``X-API-Key: <key>``). It refuses to start without a real CONDUIT_API_KEY, binds to
``api_host`` (127.0.0.1 by default), and must run behind TLS when exposed publicly.
For public hosting, also set the transport's allowed Host/Origin (DNS-rebinding
protection) via security_settings.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from conduit.core.config import settings
from conduit.mcp_server import server

# An unset/placeholder key must never authorize a wallet-controlling endpoint.
_UNSET_KEYS = {"", "CHANGE-ME"}


def _request_key(scope: Scope) -> str:
    """Extract the presented key from an Authorization bearer or X-API-Key header."""
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    auth = headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return headers.get("x-api-key", "")


def build_app() -> Starlette:
    """ASGI app that serves the MCP server over streamable-HTTP at /mcp, API-key gated."""
    manager = StreamableHTTPSessionManager(app=server)
    expected = settings.conduit_api_key

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        if expected in _UNSET_KEYS or _request_key(scope) != expected:
            await JSONResponse({"error": "unauthorized"}, status_code=401)(
                scope, receive, send
            )
            return
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=handle_mcp)], lifespan=lifespan)


def run() -> None:
    """Console entry point (conduit-mcp-http): serve MCP over streamable-HTTP."""
    import uvicorn

    if settings.conduit_api_key in _UNSET_KEYS:
        raise SystemExit(
            "Refusing to start the HTTP MCP transport without a real CONDUIT_API_KEY "
            "(it would expose wallet-controlling tools unauthenticated)."
        )
    uvicorn.run(build_app(), host=settings.api_host, port=settings.mcp_http_port)
