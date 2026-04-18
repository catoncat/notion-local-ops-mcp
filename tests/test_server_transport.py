from notion_local_ops_mcp.server import build_http_app


def test_http_app_uses_streamable_http_transport() -> None:
    app = build_http_app()

    assert app.state.transport_type == "streamable-http"
    assert app.state.path == "/mcp"
