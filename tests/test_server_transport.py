from __future__ import annotations

import contextlib
import socket
import threading
import time
from pathlib import Path

import anyio
import httpx
import uvicorn
from starlette.testclient import TestClient

from notion_local_ops_mcp import server
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
