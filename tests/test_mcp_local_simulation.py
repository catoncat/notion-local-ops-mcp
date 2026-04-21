from __future__ import annotations

import contextlib
import socket
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from notion_local_ops_mcp.executors import ExecutorRegistry
from notion_local_ops_mcp.tasks import TaskStore
from tests.helpers import python_json_cmd, python_print_cmd, python_sleep_cmd


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _running_server(
    tmp_path: Path,
    monkeypatch,
    *,
    auth_token: str,
    codex_command: str = python_print_cmd("codex"),
    claude_command: str = python_print_cmd("claude"),
):
    from notion_local_ops_mcp import server

    monkeypatch.setattr(server, "AUTH_TOKEN", auth_token)
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=codex_command,
        claude_command=claude_command,
    )
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "registry", registry)

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
        raise AssertionError("Timed out waiting for MCP test server to start.")

    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "uvicorn test server did not shut down cleanly"


@asynccontextmanager
async def _mcp_session(url: str, *, token: str):
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
        async with streamable_http_client(url, http_client=client) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


async def _call_tool(session: ClientSession, name: str, arguments: dict[str, object]) -> dict[str, object]:
    result = await session.call_tool(name, arguments)
    assert result.isError is False, result
    assert result.structuredContent is not None
    return result.structuredContent


def test_mcp_run_command_stream_end_to_end(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                queued = await _call_tool(
                    session,
                    "run_command_stream",
                    {
                        "command": python_print_cmd("stream-ok"),
                        "timeout": 5,
                    },
                )
                result = await _call_tool(
                    session,
                    "wait_task",
                    {
                        "task_id": queued["task_id"],
                        "timeout": 5,
                        "poll_interval": 0.05,
                    },
                )
                assert queued["stream_mode"] == "task-polling"
                assert result["status"] == "succeeded"
                assert "stream-ok" in result["stdout_tail"]

        anyio.run(scenario)


def test_mcp_run_command_stream_timeout_end_to_end(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                queued = await _call_tool(
                    session,
                    "run_command_stream",
                    {
                        "command": python_sleep_cmd(2),
                        "timeout": 1,
                    },
                )
                result = await _call_tool(
                    session,
                    "wait_task",
                    {
                        "task_id": queued["task_id"],
                        "timeout": 5,
                        "poll_interval": 0.05,
                    },
                )
                final_meta = await _call_tool(
                    session,
                    "get_task",
                    {"task_id": queued["task_id"]},
                )
                assert result["status"] == "failed"
                assert final_meta.get("timed_out") is True

        anyio.run(scenario)


def test_mcp_delegate_task_structured_output_end_to_end(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    codex_command = python_json_cmd({"ok": True, "source": "delegate"})
    with _running_server(
        tmp_path,
        monkeypatch,
        auth_token=token,
        codex_command=codex_command,
    ) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                queued = await _call_tool(
                    session,
                    "delegate_task",
                    {
                        "task": "emit json",
                        "executor": "codex",
                        "output_schema": {"type": "object"},
                        "parse_structured_output": True,
                    },
                )
                result = await _call_tool(
                    session,
                    "wait_task",
                    {
                        "task_id": queued["task_id"],
                        "timeout": 5,
                        "poll_interval": 0.05,
                    },
                )
                assert result["status"] == "succeeded"
                assert result["structured_output"] == {"ok": True, "source": "delegate"}

        anyio.run(scenario)


def test_mcp_canonical_search_and_read_text_end_to_end(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    (tmp_path / "demo.py").write_text("alpha\nTODO item\n", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("TODO hidden\n", encoding="utf-8")

    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                found = await _call_tool(
                    session,
                    "search",
                    {
                        "mode": "glob",
                        "path": ".",
                        "pattern": "*.py",
                    },
                )
                read = await _call_tool(
                    session,
                    "read_text",
                    {
                        "path": "demo.py",
                        "start_line": 2,
                        "line_limit": 1,
                        "include_line_numbers": True,
                    },
                )
                single_file_search = await _call_tool(
                    session,
                    "search",
                    {
                        "mode": "text",
                        "path": "demo.py",
                        "query": "TODO",
                    },
                )
                assert found["success"] is True
                assert found["mode"] == "glob"
                assert any(item["path"].endswith("demo.py") for item in found["matches"])
                assert all(not item["path"].endswith(".hidden.py") for item in found["matches"])
                assert read["success"] is True
                assert read["mode"] == "single"
                assert read["content"] == "2: TODO item"
                assert single_file_search["success"] is True
                assert single_file_search["mode"] == "text"
                assert len(single_file_search["matches"]) == 1
                assert single_file_search["matches"][0]["path"].endswith("demo.py")

        anyio.run(scenario)
