from __future__ import annotations

import contextlib
import socket
import threading
import time
from pathlib import Path

import anyio
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _running_server(tmp_path: Path, monkeypatch):
    from notion_local_ops_mcp import server

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
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    else:
        raise AssertionError("Timed out waiting for test MCP server to start.")

    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)


async def _connect_and_initialize(url: str) -> list[str]:
    async with streamable_http_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            return [tool.name for tool in tools.tools]


async def _call_tool_structured(
    session: ClientSession,
    tool_name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    result = await session.call_tool(tool_name, arguments)
    assert result.isError is False
    assert result.structuredContent is not None
    return result.structuredContent


def test_single_server_accepts_two_concurrent_clients(tmp_path: Path, monkeypatch) -> None:
    with _running_server(tmp_path, monkeypatch) as url:
        async def scenario() -> tuple[list[str], list[str]]:
            async with anyio.create_task_group() as task_group:
                results: list[list[str] | None] = [None, None]

                async def run_client(index: int) -> None:
                    results[index] = await _connect_and_initialize(url)

                task_group.start_soon(run_client, 0)
                task_group.start_soon(run_client, 1)

            assert results[0] is not None
            assert results[1] is not None
            return results[0], results[1]

        first_tools, second_tools = anyio.run(scenario)

    assert "list_files" in first_tools
    assert "list_files" in second_tools
    assert "write_file" in first_tools
    assert "write_file" in second_tools


def test_two_clients_share_one_mutable_workspace_state(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "shared.txt"

    with _running_server(tmp_path, monkeypatch) as url:
        async def scenario() -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
            async with streamable_http_client(url) as first_streams:
                async with ClientSession(first_streams[0], first_streams[1]) as first_session:
                    async with streamable_http_client(url) as second_streams:
                        async with ClientSession(second_streams[0], second_streams[1]) as second_session:
                            await first_session.initialize()
                            await second_session.initialize()

                            first_write = await _call_tool_structured(
                                first_session,
                                "write_file",
                                {
                                    "path": "shared.txt",
                                    "content": "alpha",
                                },
                            )
                            second_read = await _call_tool_structured(
                                second_session,
                                "read_file",
                                {"path": "shared.txt"},
                            )
                            second_write = await _call_tool_structured(
                                second_session,
                                "write_file",
                                {
                                    "path": "shared.txt",
                                    "content": "beta",
                                },
                            )
                            first_read = await _call_tool_structured(
                                first_session,
                                "read_file",
                                {"path": "shared.txt"},
                            )
                            return first_write, second_read, second_write, first_read

        first_write, second_read, second_write, first_read = anyio.run(scenario)

    assert first_write["success"] is True
    assert second_read["success"] is True
    assert second_read["content"] == "alpha"
    assert second_write["success"] is True
    assert first_read["success"] is True
    assert first_read["content"] == "beta"
    assert target.read_text(encoding="utf-8") == "beta"
