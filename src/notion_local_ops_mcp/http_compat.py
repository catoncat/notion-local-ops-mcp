from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Any, AsyncIterator, Callable

from starlette.applications import Starlette
from starlette.middleware import Middleware as StarletteMiddleware
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

SERVER_CARD_SCHEMA = "https://static.modelcontextprotocol.io/schemas/mcp-server-card/v1.json"
PROTOCOL_VERSION = "2025-06-18"
DISCOVERY_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept",
    "Cache-Control": "public, max-age=300",
}
MCP_METHOD_HEADERS = {
    "Allow": "GET, POST, DELETE, HEAD, OPTIONS",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept",
}
LEGACY_SSE_MESSAGE_PATHS = {"/messages", "/messages/"}
DISCOVERY_PATHS = {"/.well-known/mcp.json", "/.well-known/mcp/server-card.json"}
OAUTH_DISCOVERY_PATHS = {"/.well-known/oauth-authorization-server"}

AuthTokenProvider = Callable[[], str]


def _resolve_version(app_name: str) -> str:
    try:
        return package_version(app_name)
    except PackageNotFoundError:
        return "0.0.0"


def _extract_bearer_token(authorization: str) -> str:
    value = (authorization or "").strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def _build_server_card(
    *,
    app_name: str,
    app_version: str,
    mcp_path: str,
    auth_required: bool,
    instructions: str,
) -> dict[str, Any]:
    return {
        "$schema": SERVER_CARD_SCHEMA,
        "protocolVersion": PROTOCOL_VERSION,
        "serverInfo": {
            "name": app_name,
            "title": app_name,
            "version": app_version,
        },
        "description": "Local MCP server for filesystem, shell, git, and delegated coding tasks.",
        "transport": {
            "type": "streamable-http",
            "endpoint": mcp_path,
        },
        "capabilities": {
            "tools": {"listChanged": True},
        },
        "authentication": {
            "required": auth_required,
            "schemes": ["bearer"] if auth_required else [],
        },
        "instructions": instructions,
    }


class HTTPBearerAuthMiddleware:
    """HTTP-layer Bearer auth.

    Applied before any MCP/SSE transport handling so that unauthenticated clients
    cannot open SSE sessions or queue legacy /messages payloads. MCP discovery,
    HEAD probes, OAuth discovery probes, and OPTIONS preflights are always
    allowed.
    """

    def __init__(self, app: Any, get_auth_token: AuthTokenProvider) -> None:
        self.app = app
        self._get_auth_token = get_auth_token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if method in {"OPTIONS", "HEAD"} or path in DISCOVERY_PATHS or path in OAUTH_DISCOVERY_PATHS:
            await self.app(scope, receive, send)
            return

        expected = (self._get_auth_token() or "").strip()
        if not expected:
            await self.app(scope, receive, send)
            return

        provided = _extract_bearer_token(Headers(raw=scope.get("headers", [])).get("authorization", ""))
        if provided != expected:
            response = JSONResponse(
                {"error": "unauthorized", "message": "Missing or invalid bearer token."},
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="mcp"',
                    "Access-Control-Allow-Origin": "*",
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class MCPCompatibilityDispatcher:
    def __init__(
        self,
        *,
        streamable_app: Any,
        legacy_sse_app: Any,
        app_name: str,
        app_version: str,
        mcp_path: str,
        get_auth_token: AuthTokenProvider,
        instructions: str,
    ) -> None:
        self.streamable_app = streamable_app
        self.legacy_sse_app = legacy_sse_app
        self.app_name = app_name
        self.app_version = app_version
        self.mcp_path = mcp_path
        self._get_auth_token = get_auth_token
        self.instructions = instructions

    @property
    def auth_required(self) -> bool:
        return bool((self._get_auth_token() or "").strip())

    @property
    def server_card(self) -> dict[str, Any]:
        return _build_server_card(
            app_name=self.app_name,
            app_version=self.app_version,
            mcp_path=self.mcp_path,
            auth_required=self.auth_required,
            instructions=self.instructions,
        )

    @asynccontextmanager
    async def lifespan(self, _app: Any) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for child_app in (self.streamable_app, self.legacy_sse_app):
                child_lifespan = getattr(child_app, "lifespan", None)
                if child_lifespan is not None:
                    # Pass the child app itself so FastMCP sets state on its own
                    # ASGI instance rather than the outer compat app.
                    await stack.enter_async_context(child_lifespan(child_app))
            yield

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.streamable_app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        method = str(scope.get("method", "GET")).upper()
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }

        if path in LEGACY_SSE_MESSAGE_PATHS:
            await self.legacy_sse_app(scope, receive, send)
            return

        if path != self.mcp_path:
            await self.streamable_app(scope, receive, send)
            return

        if method == "HEAD":
            await Response(status_code=204, headers=MCP_METHOD_HEADERS)(scope, receive, send)
            return

        if method == "OPTIONS":
            await Response(status_code=204, headers=MCP_METHOD_HEADERS)(scope, receive, send)
            return

        if method in {"POST", "DELETE"}:
            await self.streamable_app(scope, receive, send)
            return

        if method != "GET":
            await Response(status_code=405, headers=MCP_METHOD_HEADERS)(scope, receive, send)
            return

        accept = headers.get("accept", "").lower()
        if "text/event-stream" not in accept:
            await JSONResponse(self.server_card, headers=MCP_METHOD_HEADERS)(scope, receive, send)
            return

        session_id = headers.get("mcp-session-id", "").strip()
        if session_id:
            await self.streamable_app(scope, receive, send)
            return

        await self.legacy_sse_app(scope, receive, send)


def build_http_compat_app(
    *,
    streamable_app: Any,
    legacy_sse_app: Any,
    app_name: str,
    mcp_path: str,
    get_auth_token: AuthTokenProvider,
    instructions: str,
) -> Starlette:
    app_version = _resolve_version(app_name)
    dispatcher = MCPCompatibilityDispatcher(
        streamable_app=streamable_app,
        legacy_sse_app=legacy_sse_app,
        app_name=app_name,
        app_version=app_version,
        mcp_path=mcp_path,
        get_auth_token=get_auth_token,
        instructions=instructions,
    )

    async def server_card(_: Request) -> JSONResponse:
        return JSONResponse(dispatcher.server_card, headers=DISCOVERY_HEADERS)

    async def oauth_discovery(_: Request) -> Response:
        return Response(status_code=404, headers=DISCOVERY_HEADERS)

    app = Starlette(
        routes=[
            Route("/.well-known/mcp.json", endpoint=server_card, methods=["GET"]),
            Route("/.well-known/mcp/server-card.json", endpoint=server_card, methods=["GET"]),
            Route("/.well-known/oauth-authorization-server", endpoint=oauth_discovery, methods=["GET"]),
            Mount("/", app=dispatcher),
        ],
        middleware=[
            StarletteMiddleware(
                HTTPBearerAuthMiddleware,
                get_auth_token=get_auth_token,
            ),
        ],
        lifespan=dispatcher.lifespan,
    )

    app.state.transport_type = "streamable-http"
    app.state.path = mcp_path
    return app
