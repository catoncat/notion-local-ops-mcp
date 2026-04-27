from __future__ import annotations

import contextlib
import base64
import hashlib
import logging
import os
import secrets
import socket
import stat
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware as StarletteMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from notion_local_ops_mcp import server
from notion_local_ops_mcp.http_compat import MCPDebugLoggingMiddleware
from notion_local_ops_mcp.server import build_http_app


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _running_server(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(server, "AUTH_TOKEN", "")
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
    app = server.build_http_app()
    port = _find_free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="on",
    )
    uvicorn_server = uvicorn.Server(config)
    uvicorn_server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    ready = False
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                ready = True
                break
        time.sleep(0.05)
    if not ready:
        raise AssertionError("Timed out waiting for test MCP server to start.")

    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "uvicorn test server did not shut down cleanly"


def test_http_app_uses_streamable_http_transport() -> None:
    app = build_http_app()

    assert app.state.transport_type == "streamable-http"
    assert app.state.path == "/mcp"


def test_http_app_supports_head_on_mcp(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.head("/mcp")

    assert response.status_code == 204
    assert response.headers["allow"] == "GET, POST, DELETE, HEAD, OPTIONS"


def test_http_app_supports_options_preflight(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.options(
            "/mcp",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "POST",
            },
        )

    # Preflights must succeed even without a bearer token.
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"


def test_http_app_exposes_server_card(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/.well-known/mcp.json")

    assert response.status_code == 200
    body = response.json()
    assert body["transport"] == {"type": "streamable-http", "endpoint": "/mcp"}
    assert body["authentication"] == {"required": True, "schemes": ["bearer"]}
    # Discovery should not depend on server card revision fields that are not in the spec.
    assert "version" not in body


def test_http_app_server_card_reflects_disabled_auth(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/.well-known/mcp.json")

    assert response.status_code == 200
    assert response.json()["authentication"] == {"required": False, "schemes": []}


def test_http_app_returns_server_card_for_plain_get_mcp(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/mcp", headers={"Accept": "*/*"})

    assert response.status_code == 200
    assert response.json()["transport"]["endpoint"] == "/mcp"


def test_http_app_rejects_unauthenticated_sse_get(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/mcp", headers={"Accept": "text/event-stream"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"].lower().startswith("bearer")


def test_http_app_rejects_unauthenticated_plain_get(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/mcp", headers={"Accept": "*/*"})

    assert response.status_code == 401


def test_http_app_rejects_unauthenticated_messages_post(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.post(
            "/messages/",
            params={"session_id": "anything"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )

    assert response.status_code == 401


def test_http_app_allows_discovery_without_token(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/.well-known/mcp.json")

    assert response.status_code == 200


def test_http_app_allows_unauthenticated_head_probe(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.head("/mcp")

    assert response.status_code == 204
    assert response.headers["allow"] == "GET, POST, DELETE, HEAD, OPTIONS"


def test_http_app_accepts_valid_bearer_on_head(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.head("/mcp", headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 204


def test_http_app_does_not_require_auth_for_oauth_discovery_probe(monkeypatch) -> None:
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 404


def test_shared_token_mode_rejects_when_auth_token_is_empty(monkeypatch) -> None:
    # Guard against an empty/misconfigured AUTH_TOKEN silently allowing requests
    # that omit the Authorization header (both sides would compare equal as "").
    monkeypatch.setattr(server, "AUTH_MODE", "shared_token")
    monkeypatch.setattr(server, "AUTH_TOKEN", "")
    app = build_http_app()

    with TestClient(app) as client:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})

    assert response.status_code == 401


def test_oauth_metadata_does_not_trust_x_forwarded_host(monkeypatch, tmp_path) -> None:
    # Without PUBLIC_BASE_URL the issuer URL falls back to the request's Host
    # header, but X-Forwarded-Host must NOT be honored: a tunnel attacker could
    # otherwise redirect the OAuth metadata to a phishing host.
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get(
            "/.well-known/oauth-authorization-server",
            headers={"X-Forwarded-Host": "attacker.example", "X-Forwarded-Proto": "https"},
        )

    assert response.status_code == 200
    body = response.json()
    assert "attacker.example" not in body["issuer"]
    assert "attacker.example" not in body["authorization_endpoint"]


def test_oauth_register_enforces_client_limit(monkeypatch, tmp_path) -> None:
    from notion_local_ops_mcp.oauth import MAX_REGISTERED_CLIENTS

    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    payload = {
        "client_name": "ChatGPT",
        "redirect_uris": ["https://chat.openai.com/aip/callback"],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    with TestClient(app) as client:
        for _ in range(MAX_REGISTERED_CLIENTS):
            ok = client.post("/oauth/register", json=payload)
            assert ok.status_code == 201
        rejected = client.post("/oauth/register", json=payload)

    assert rejected.status_code == 400
    assert rejected.json()["error"] == "invalid_client_metadata"


def test_oauth_store_file_permissions_are_locked_down(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    with TestClient(app) as client:
        registration = client.post(
            "/oauth/register",
            json={
                "client_name": "ChatGPT",
                "redirect_uris": ["https://chat.openai.com/aip/callback"],
            },
        )

    assert registration.status_code == 201
    oauth_path = tmp_path / "oauth.json"
    assert oauth_path.exists()
    if os.name == "nt":
        # Windows' stat permission bits do not reflect ACL owner-only semantics;
        # Path.chmod(0o600) only toggles read-only and still reports 0o666.
        return
    file_mode = stat.S_IMODE(oauth_path.stat().st_mode)
    assert file_mode == 0o600, f"oauth.json mode={oct(file_mode)} (expected 0o600)"


def test_http_app_exposes_minimal_oauth_metadata(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    with TestClient(app) as client:
        resource = client.get("/.well-known/oauth-protected-resource/mcp")
        issuer = client.get("/.well-known/oauth-authorization-server")

    assert resource.status_code == 200
    assert resource.json()["resource"] == "https://mcp.example.test/mcp"
    assert resource.json()["authorization_servers"] == ["https://mcp.example.test"]
    assert resource.json()["scopes_supported"] == ["local-ops"]
    assert resource.json()["bearer_methods_supported"] == ["header"]

    assert issuer.status_code == 200
    issuer_body = issuer.json()
    assert issuer_body["issuer"] == "https://mcp.example.test"
    assert issuer_body["authorization_endpoint"] == "https://mcp.example.test/oauth/authorize"
    assert issuer_body["token_endpoint"] == "https://mcp.example.test/oauth/token"
    assert issuer_body["registration_endpoint"] == "https://mcp.example.test/oauth/register"
    assert issuer_body["code_challenge_methods_supported"] == ["S256"]


def test_http_app_oauth_challenge_advertises_resource_metadata(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    with TestClient(app) as client:
        response = client.get("/mcp", headers={"Accept": "text/event-stream"})

    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    expected_metadata = 'resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource/mcp"'
    assert expected_metadata in challenge
    assert 'scope="local-ops"' in challenge


def test_http_app_oauth_dcr_pkce_flow_allows_mcp_access(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "AUTH_MODE", "oauth")
    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://mcp.example.test")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    app = build_http_app()

    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    redirect_uri = "https://chat.openai.com/aip/callback"

    with TestClient(app, follow_redirects=False) as client:
        registration = client.post(
            "/oauth/register",
            json={
                "client_name": "ChatGPT",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert registration.status_code == 201
        client_id = registration.json()["client_id"]

        authorize = client.post(
            "/oauth/authorize",
            data={
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "state": "state-123",
                "scope": "local-ops",
                "resource": "https://mcp.example.test/mcp",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "login_token": "secret-token",
            },
        )
        assert authorize.status_code == 303
        parsed_redirect = urlparse(authorize.headers["location"])
        params = parse_qs(parsed_redirect.query)
        assert parsed_redirect.geturl().startswith(redirect_uri)
        assert params["state"] == ["state-123"]
        code = params["code"][0]

        token = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": "https://mcp.example.test/mcp",
            },
        )
        assert token.status_code == 200
        access_token = token.json()["access_token"]
        assert token.json()["token_type"] == "Bearer"

        response = client.get(
            "/mcp",
            headers={"Accept": "*/*", "Authorization": f"Bearer {access_token}"},
        )
        assert response.status_code == 200
        assert response.json()["transport"]["endpoint"] == "/mcp"


def test_http_app_supports_legacy_sse_get_on_mcp(tmp_path, monkeypatch) -> None:
    with _running_server(tmp_path, monkeypatch) as url:
        async def scenario() -> None:
            async with httpx.AsyncClient(timeout=5.0) as client:
                async with client.stream(
                    "GET",
                    url,
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("text/event-stream")

        anyio.run(scenario)


def test_debug_logging_middleware_logs_rpc_method_and_preserves_body(caplog) -> None:
    async def echo_json(request) -> JSONResponse:
        return JSONResponse(await request.json())

    app = Starlette(
        routes=[Route("/mcp", endpoint=echo_json, methods=["POST"])],
        middleware=[
            StarletteMiddleware(
                MCPDebugLoggingMiddleware,
                get_debug_enabled=lambda: True,
                mcp_path="/mcp",
            )
        ],
    )

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search", "arguments": {"mode": "text", "query": "TODO"}},
    }

    caplog.set_level(logging.INFO, logger="notion_local_ops_mcp.mcp_debug")
    with TestClient(app) as client:
        response = client.post("/mcp", json=payload, headers={"mcp-session-id": "sess-123"})

    assert response.status_code == 200
    assert response.json() == payload
    messages = [record.message for record in caplog.records if record.name == "notion_local_ops_mcp.mcp_debug"]
    assert any("phase=request" in message and '"method":"tools/call"' in message for message in messages)
    assert any('"tool":"search"' in message for message in messages)
    assert any('"tool_args":"{\\"mode\\":\\"text\\",\\"query\\":\\"TODO\\"}"' in message for message in messages)
    assert any("phase=response_end" in message and "status=200" in message for message in messages)


def test_http_app_debug_logging_does_not_break_streamable_http_initialize(tmp_path, monkeypatch) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    monkeypatch.setattr(server, "AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(server, "DEBUG_MCP_LOGGING", True)

    with _running_server(tmp_path, monkeypatch) as url:

        async def scenario() -> None:
            headers = {"Authorization": "Bearer secret-token"}
            async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
                async with streamable_http_client(url, http_client=client) as (
                    read_stream,
                    write_stream,
                    _,
                ):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()

        anyio.run(scenario)
