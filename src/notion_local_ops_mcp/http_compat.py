from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import logging
import sys
import time
from typing import Any, AsyncIterator, Callable
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware as StarletteMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from .oauth import OAuthManager, OAuthRuntimeConfig

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
OAUTH_DISCOVERY_PATHS = {
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
}

AuthTokenProvider = Callable[[], str]
OAuthConfigProvider = Callable[[], OAuthRuntimeConfig]
DebugEnabledProvider = Callable[[], bool]
DEBUG_LOGGER = logging.getLogger("notion_local_ops_mcp.mcp_debug")


def _emit_debug_log(message: str, *args: object) -> None:
    DEBUG_LOGGER.info(message, *args)
    rendered = message % args if args else message
    sys.stderr.write(f"{rendered}\n")
    sys.stderr.flush()


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


def _base_url_from_headers(headers: Headers, scheme: str = "https") -> str:
    forwarded_proto = headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    forwarded_host = headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    proto = forwarded_proto or scheme
    host = forwarded_host or headers.get("host", "")
    return f"{proto}://{host}".rstrip("/")


async def _parse_request_data(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await request.json()
        return payload if isinstance(payload, dict) else {}
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _string_values(data: dict[str, Any]) -> dict[str, str]:
    return {key: str(value) for key, value in data.items() if value is not None}


def _build_server_card(
    *,
    app_name: str,
    app_version: str,
    mcp_path: str,
    auth_schemes: list[str],
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
            "required": bool(auth_schemes),
            "schemes": auth_schemes,
        },
        "instructions": instructions,
    }


def _extract_session_hint(scope: dict[str, Any]) -> str | None:
    headers = Headers(raw=scope.get("headers", []))
    session_id = headers.get("mcp-session-id", "").strip()
    if session_id:
        return session_id
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    query_session = (query.get("session_id") or [""])[0].strip()
    return query_session or None


def _truncate_jsonish(value: Any, *, max_chars: int = 240) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        rendered = repr(value)
    if len(rendered) <= max_chars:
        return rendered
    return rendered[: max_chars - 1] + "…"


def _summarize_rpc_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {"kind": "empty"}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"kind": "non_json", "bytes": len(body)}

    items = payload if isinstance(payload, list) else [payload]
    entries: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            entries.append({"kind": type(item).__name__})
            continue
        method = item.get("method")
        params = item.get("params")
        tool_name = None
        tool_args = None
        params_summary = None
        if isinstance(params, dict):
            tool_name = params.get("name") or params.get("tool")
            if "arguments" in params:
                tool_args = _truncate_jsonish(params.get("arguments"))
            else:
                params_summary = _truncate_jsonish(params)
        entries.append(
            {
                "id": item.get("id"),
                "method": method,
                "tool": tool_name,
                "tool_args": tool_args,
                "params_summary": params_summary,
            }
        )
    return {
        "kind": "jsonrpc",
        "batch": isinstance(payload, list),
        "count": len(items),
        "entries": entries,
    }


class MCPDebugLoggingMiddleware:
    def __init__(
        self,
        app: Any,
        *,
        get_debug_enabled: DebugEnabledProvider,
        mcp_path: str,
    ) -> None:
        self.app = app
        self._get_debug_enabled = get_debug_enabled
        self._mcp_path = mcp_path

    def _should_trace(self, scope: dict[str, Any]) -> bool:
        if scope.get("type") != "http":
            return False
        path = str(scope.get("path", ""))
        return (
            path == self._mcp_path
            or path in LEGACY_SSE_MESSAGE_PATHS
            or path in DISCOVERY_PATHS
            or path.startswith("/.well-known/oauth-")
            or path.startswith("/oauth/")
            or path in {"/authorize", "/token"}
        )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not self._get_debug_enabled() or not self._should_trace(scope):
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        client = scope.get("client") or ("", 0)
        client_host = client[0] if isinstance(client, tuple) else str(client)
        session_hint = _extract_session_hint(scope)
        headers = Headers(raw=scope.get("headers", []))
        accept = headers.get("accept", "")
        request_id = hex(time.monotonic_ns())[-10:]
        rpc_summary: dict[str, Any] | None = None
        request_logged = False
        body_parts: list[bytes] = []

        if method not in {"POST", "DELETE"}:
            _emit_debug_log(
                "MCP_DEBUG request_id=%s phase=request method=%s path=%s client=%s session=%s accept=%s",
                request_id,
                method,
                path,
                client_host,
                session_hint or "-",
                accept or "-",
            )

        status_code: int | None = None
        stream_logged = False

        async def receive_wrapper() -> dict[str, Any]:
            nonlocal request_logged, rpc_summary
            message = await receive()
            if method in {"POST", "DELETE"} and message["type"] == "http.request":
                body_parts.append(message.get("body", b""))
                if not message.get("more_body", False) and not request_logged:
                    body_bytes = b"".join(body_parts)
                    rpc_summary = _summarize_rpc_body(body_bytes)
                    _emit_debug_log(
                        "MCP_DEBUG request_id=%s phase=request method=%s path=%s client=%s session=%s body_bytes=%s rpc=%s",
                        request_id,
                        method,
                        path,
                        client_host,
                        session_hint or "-",
                        len(body_bytes),
                        json.dumps(rpc_summary, ensure_ascii=False, separators=(",", ":")),
                    )
                    request_logged = True
            elif method in {"POST", "DELETE"} and message["type"] == "http.disconnect" and not request_logged:
                body_bytes = b"".join(body_parts)
                rpc_summary = _summarize_rpc_body(body_bytes)
                _emit_debug_log(
                    "MCP_DEBUG request_id=%s phase=request_disconnected method=%s path=%s client=%s session=%s body_bytes=%s rpc=%s",
                    request_id,
                    method,
                    path,
                    client_host,
                    session_hint or "-",
                    len(body_bytes),
                    json.dumps(rpc_summary, ensure_ascii=False, separators=(",", ":")),
                )
                request_logged = True
            return message

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code, stream_logged
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                if method == "GET" and "text/event-stream" in accept.lower():
                    stream_logged = True
                _emit_debug_log(
                    "MCP_DEBUG request_id=%s phase=response_start method=%s path=%s session=%s status=%s stream=%s",
                    request_id,
                    method,
                    path,
                    session_hint or "-",
                    status_code,
                    stream_logged,
                )
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                duration_ms = round((time.monotonic() - started) * 1000, 1)
                _emit_debug_log(
                    "MCP_DEBUG request_id=%s phase=response_end method=%s path=%s session=%s status=%s duration_ms=%s stream_started=%s",
                    request_id,
                    method,
                    path,
                    session_hint or "-",
                    status_code,
                    duration_ms,
                    stream_logged,
                )
            await send(message)

        await self.app(scope, receive_wrapper, send_wrapper)


class HTTPBearerAuthMiddleware:
    """HTTP-layer Bearer auth.

    Applied before any MCP/SSE transport handling so that unauthenticated clients
    cannot open SSE sessions or queue legacy /messages payloads. MCP discovery,
    HEAD probes, OAuth discovery probes, and OPTIONS preflights are always
    allowed.
    """

    def __init__(
        self,
        app: Any,
        get_auth_token: AuthTokenProvider,
        get_oauth_config: OAuthConfigProvider,
        oauth_manager: OAuthManager,
        mcp_path: str,
    ) -> None:
        self.app = app
        self._get_auth_token = get_auth_token
        self._get_oauth_config = get_oauth_config
        self._oauth_manager = oauth_manager
        self._mcp_path = mcp_path

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if (
            method in {"OPTIONS", "HEAD"}
            or path in DISCOVERY_PATHS
            or path in OAUTH_DISCOVERY_PATHS
            or path.startswith("/oauth/")
        ):
            await self.app(scope, receive, send)
            return

        config = self._get_oauth_config()
        auth_mode = config.normalized_auth_mode
        if auth_mode == "none":
            await self.app(scope, receive, send)
            return

        provided = _extract_bearer_token(Headers(raw=scope.get("headers", [])).get("authorization", ""))
        if auth_mode == "shared_token":
            if provided == (self._get_auth_token() or "").strip():
                await self.app(scope, receive, send)
                return
            await self._unauthorized(scope, receive, send, oauth=False)
            return

        if auth_mode == "oauth":
            headers = Headers(raw=scope.get("headers", []))
            fallback_base_url = _base_url_from_headers(headers, str(scope.get("scheme", "https")))
            base_url = self._oauth_manager.metadata_base_url(fallback_base_url)
            static_token_matches = bool(provided and config.auth_token and provided == config.auth_token)
            oauth_token_matches = self._oauth_manager.verify_access_token(provided, base_url=base_url)
            if static_token_matches or oauth_token_matches:
                await self.app(scope, receive, send)
                return
            await self._unauthorized(scope, receive, send, oauth=True, base_url=base_url)
            return

        await self._unauthorized(scope, receive, send, oauth=False)

    async def _unauthorized(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        *,
        oauth: bool,
        base_url: str | None = None,
    ) -> None:
        if oauth and base_url:
            challenge = (
                f'Bearer realm="mcp", '
                f'resource_metadata="{self._oauth_manager.resource_metadata_url(base_url)}", '
                f'scope="{self._oauth_manager.scope_string()}"'
            )
        else:
            challenge = 'Bearer realm="mcp"'
        response = JSONResponse(
            {"error": "unauthorized", "message": "Missing or invalid bearer token."},
            status_code=401,
            headers={
                "WWW-Authenticate": challenge,
                "Access-Control-Allow-Origin": "*",
            },
        )
        await response(scope, receive, send)


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
        get_oauth_config: OAuthConfigProvider,
        instructions: str,
    ) -> None:
        self.streamable_app = streamable_app
        self.legacy_sse_app = legacy_sse_app
        self.app_name = app_name
        self.app_version = app_version
        self.mcp_path = mcp_path
        self._get_auth_token = get_auth_token
        self._get_oauth_config = get_oauth_config
        self.instructions = instructions

    @property
    def auth_schemes(self) -> list[str]:
        config = self._get_oauth_config()
        mode = config.normalized_auth_mode
        if mode == "none":
            return []
        if mode == "oauth":
            return ["oauth2", "bearer"]
        return ["bearer"] if (self._get_auth_token() or "").strip() else []

    @property
    def server_card(self) -> dict[str, Any]:
        return _build_server_card(
            app_name=self.app_name,
            app_version=self.app_version,
            mcp_path=self.mcp_path,
            auth_schemes=self.auth_schemes,
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
    get_oauth_config: OAuthConfigProvider,
    get_debug_enabled: DebugEnabledProvider,
    instructions: str,
) -> Starlette:
    app_version = _resolve_version(app_name)
    oauth_manager = OAuthManager(get_oauth_config(), mcp_path=mcp_path)
    dispatcher = MCPCompatibilityDispatcher(
        streamable_app=streamable_app,
        legacy_sse_app=legacy_sse_app,
        app_name=app_name,
        app_version=app_version,
        mcp_path=mcp_path,
        get_auth_token=get_auth_token,
        get_oauth_config=get_oauth_config,
        instructions=instructions,
    )

    def oauth_base_url(request: Request) -> str:
        fallback = _base_url_from_headers(request.headers, request.url.scheme)
        return oauth_manager.metadata_base_url(fallback)

    def oauth_enabled() -> bool:
        return get_oauth_config().normalized_auth_mode == "oauth"

    async def server_card(_: Request) -> JSONResponse:
        return JSONResponse(dispatcher.server_card, headers=DISCOVERY_HEADERS)

    async def oauth_authorization_server_metadata(request: Request) -> Response:
        if not oauth_enabled():
            return Response(status_code=404, headers=DISCOVERY_HEADERS)
        return JSONResponse(
            oauth_manager.authorization_server_metadata(oauth_base_url(request)),
            headers=DISCOVERY_HEADERS,
        )

    async def oauth_protected_resource_metadata(request: Request) -> Response:
        if not oauth_enabled():
            return Response(status_code=404, headers=DISCOVERY_HEADERS)
        return JSONResponse(
            oauth_manager.protected_resource_metadata(oauth_base_url(request)),
            headers=DISCOVERY_HEADERS,
        )

    async def oauth_register(request: Request) -> Response:
        if not oauth_enabled():
            return Response(status_code=404, headers=DISCOVERY_HEADERS)
        try:
            registration = oauth_manager.register_client(await _parse_request_data(request))
        except ValueError as exc:
            return JSONResponse(
                {"error": "invalid_client_metadata", "error_description": str(exc)},
                status_code=400,
            )
        return JSONResponse(registration, status_code=201)

    async def oauth_authorize(request: Request) -> Response:
        if not oauth_enabled():
            return Response(status_code=404, headers=DISCOVERY_HEADERS)
        if request.method == "GET":
            return HTMLResponse(oauth_manager.authorize_page(dict(request.query_params)))
        try:
            redirect_url = oauth_manager.authorize(
                _string_values(await _parse_request_data(request)),
                base_url=oauth_base_url(request),
            )
        except PermissionError as exc:
            return JSONResponse({"error": "access_denied", "error_description": str(exc)}, status_code=401)
        except ValueError as exc:
            return JSONResponse({"error": "invalid_request", "error_description": str(exc)}, status_code=400)
        return RedirectResponse(redirect_url, status_code=303)

    async def oauth_token(request: Request) -> Response:
        if not oauth_enabled():
            return Response(status_code=404, headers=DISCOVERY_HEADERS)
        try:
            token = oauth_manager.exchange_code(
                _string_values(await _parse_request_data(request)),
                base_url=oauth_base_url(request),
            )
        except ValueError as exc:
            return JSONResponse({"error": "invalid_grant", "error_description": str(exc)}, status_code=400)
        return JSONResponse(token)

    app = Starlette(
        routes=[
            Route("/.well-known/mcp.json", endpoint=server_card, methods=["GET"]),
            Route("/.well-known/mcp/server-card.json", endpoint=server_card, methods=["GET"]),
            Route(
                "/.well-known/oauth-authorization-server",
                endpoint=oauth_authorization_server_metadata,
                methods=["GET"],
            ),
            Route(
                "/.well-known/openid-configuration",
                endpoint=oauth_authorization_server_metadata,
                methods=["GET"],
            ),
            Route(
                "/.well-known/oauth-protected-resource",
                endpoint=oauth_protected_resource_metadata,
                methods=["GET"],
            ),
            Route(
                "/.well-known/oauth-protected-resource/mcp",
                endpoint=oauth_protected_resource_metadata,
                methods=["GET"],
            ),
            Route("/oauth/register", endpoint=oauth_register, methods=["POST"]),
            Route("/oauth/authorize", endpoint=oauth_authorize, methods=["GET", "POST"]),
            Route("/oauth/token", endpoint=oauth_token, methods=["POST"]),
            Mount("/", app=dispatcher),
        ],
        middleware=[
            StarletteMiddleware(
                MCPDebugLoggingMiddleware,
                get_debug_enabled=get_debug_enabled,
                mcp_path=mcp_path,
            ),
            StarletteMiddleware(
                HTTPBearerAuthMiddleware,
                get_auth_token=get_auth_token,
                get_oauth_config=get_oauth_config,
                oauth_manager=oauth_manager,
                mcp_path=mcp_path,
            ),
        ],
        lifespan=dispatcher.lifespan,
    )

    app.state.transport_type = "streamable-http"
    app.state.path = mcp_path
    return app
