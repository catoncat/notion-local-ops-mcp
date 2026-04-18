from __future__ import annotations

import asyncio

from pathlib import Path

from notion_local_ops_mcp.executors import ExecutorRegistry
from notion_local_ops_mcp.tasks import TaskStore
from tests.helpers import commit_file, init_git_repo, python_command


def test_server_tool_surface_includes_explicit_delegate_tools() -> None:
    from notion_local_ops_mcp import server

    tools = asyncio.run(server.mcp.list_tools(run_middleware=False))
    names = {tool.name for tool in tools}

    assert {
        "delegate_task",
        "delegate_doctor",
        "codex_exec",
        "claude_exec",
        "claudecode_exec",
        "codex_review",
        "claude_review",
        "list_tasks",
    }.issubset(names)


def test_server_apply_patch_tool_updates_file(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    target = tmp_path / "note.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")

    result = server.apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                f"*** Update File: {target}",
                "@@",
                " hello",
                "-world",
                "+there",
                "*** End Patch",
            ]
        )
    )

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "hello\nthere\n"


def test_server_run_command_can_dispatch_background_tasks(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    server.registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command=python_command("print('codex')"),
        claude_command=python_command("print('claude')"),
    )

    queued = server.run_command(
        command=python_command("print('background')"),
        cwd=str(tmp_path),
        timeout=5,
        run_in_background=True,
    )
    result = server.wait_task(queued["task_id"], timeout=2, poll_interval=0.05)

    assert queued["executor"] == "shell"
    assert queued["status"] == "queued"
    assert result["status"] == "succeeded"
    assert "background" in result["stdout_tail"]


def test_server_read_files_tool_returns_multiple_file_results(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("alpha\n", encoding="utf-8")
    second.write_text("beta\n", encoding="utf-8")

    result = server.read_files(paths=[str(first), str(second)])

    assert result["success"] is True
    assert [item["content"] for item in result["results"]] == ["alpha", "beta"]


def test_server_delegate_task_accepts_structured_fields(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    server.registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command=python_command("print('codex')"),
        claude_command=python_command("print('claude')"),
    )

    queued = server.delegate_task(
        task="Implement the fallback flow",
        goal="Ship a working fallback task runner",
        instructions="Prefer explicit tools.",
        cwd=str(tmp_path),
        acceptance_criteria=["Tool returns structured status"],
        verification_commands=["pytest -q"],
        commit_mode="allowed",
    )
    meta = server.get_task(queued["task_id"])

    assert meta["goal"] == "Ship a working fallback task runner"
    assert meta["instructions"] == "Prefer explicit tools."
    assert meta["acceptance_criteria"] == ["Tool returns structured status"]
    assert meta["verification_commands"] == ["pytest -q"]
    assert meta["commit_mode"] == "allowed"


def test_server_codex_review_rejects_non_git_repo(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    server.registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command=python_command("print('codex')"),
        claude_command=python_command("print('claude')"),
    )

    result = server.codex_review(cwd=str(tmp_path), commit="HEAD")

    assert result["success"] is False
    assert result["error"]["code"] == "not_a_git_repo"


def test_server_claudecode_exec_alias_queues_claude_executor(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    server.registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command=python_command("print('codex')"),
        claude_command=python_command("print('claude')"),
    )

    queued = server.claudecode_exec(task="Review alias", cwd=str(tmp_path), timeout=5)
    meta = server.get_task(queued["task_id"])

    assert queued["success"] is True
    assert meta["selected_executor"] == "claude-code"


def test_server_list_tasks_exposes_recent_entries(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    server.registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command=python_command("print('codex')"),
        claude_command=python_command("print('claude')"),
    )

    queued = server.codex_exec(task="List me", cwd=str(tmp_path), timeout=5)
    listing = server.list_tasks(limit=10)

    assert listing["success"] is True
    assert any(item["task_id"] == queued["task_id"] for item in listing["items"])


def test_server_codex_review_queues_review_task_with_git_range(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    init_git_repo(tmp_path)
    base = commit_file(tmp_path, "note.txt", "one\n", "first")
    commit_file(tmp_path, "note.txt", "two\n", "second")

    server.registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command=python_command("print('codex')"),
        claude_command=python_command("print('claude')"),
    )

    queued = server.codex_review(cwd=str(tmp_path), base_ref=base, head_ref="HEAD")
    meta = server.get_task(queued["task_id"])

    assert queued["mode"] == "review"
    assert meta["mode"] == "review"
    assert meta["split_strategy"] == "by_commit"
