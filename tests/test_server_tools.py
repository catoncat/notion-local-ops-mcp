from __future__ import annotations

import asyncio
import json
from pathlib import Path

from notion_local_ops_mcp.executors import ExecutorRegistry
from notion_local_ops_mcp.tasks import TaskStore


def _call(tool, *args, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def test_server_apply_patch_tool_updates_file(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    target = tmp_path / "note.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")

    result = _call(
        server.apply_patch,
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
        codex_command="python3 -c \"print('codex')\"",
        claude_command="python3 -c \"print('claude')\"",
    )

    queued = _call(
        server.run_command,
        command="python3 -c \"print('background')\"",
        cwd=str(tmp_path),
        timeout=5,
        run_in_background=True,
    )
    result = _call(server.wait_task, queued["task_id"], timeout=2, poll_interval=0.05)

    assert queued["executor"] == "shell"
    assert queued["status"] == "queued"
    assert result["status"] == "succeeded"
    assert "background" in result["stdout_tail"]


def test_server_read_text_tool_returns_multiple_file_results(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("alpha\n", encoding="utf-8")
    second.write_text("beta\n", encoding="utf-8")

    result = _call(server.read_text, paths=[str(first), str(second)])

    assert result["success"] is True
    assert result["mode"] == "batch"
    assert [item["content"] for item in result["results"]] == ["alpha", "beta"]


def test_server_search_tool_unifies_regex_text_and_glob(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    first = tmp_path / "one.py"
    second = tmp_path / "two.txt"
    first.write_text("alpha\nTODO: fix me\n", encoding="utf-8")
    second.write_text("beta\nTODO: docs\n", encoding="utf-8")

    glob_result = _call(server.search, mode="glob", path=str(tmp_path), pattern="*.py")
    text_result = _call(server.search, mode="text", path=str(tmp_path), query="TODO", limit=10)
    regex_result = _call(
        server.search,
        mode="regex",
        path=str(tmp_path),
        pattern=r"TODO:\s+\w+",
        output_mode="files_with_matches",
    )

    assert glob_result["success"] is True
    assert glob_result["mode"] == "glob"
    assert [Path(item["path"]).name for item in glob_result["matches"]] == ["one.py"]

    assert text_result["success"] is True
    assert text_result["mode"] == "text"
    assert len(text_result["matches"]) == 2

    assert regex_result["success"] is True
    assert regex_result["mode"] == "regex"
    assert {Path(path).name for path in regex_result["files"]} == {"one.py", "two.txt"}


def test_server_read_text_supports_single_and_batch_modes(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("alpha\nbeta\n", encoding="utf-8")
    second.write_text("gamma\ndelta\n", encoding="utf-8")

    single = _call(server.read_text, path=str(first), start_line=2, line_limit=1)
    batch = _call(
        server.read_text,
        paths=[str(first), str(second)],
        start_line=1,
        line_limit=1,
    )

    assert single["success"] is True
    assert single["mode"] == "single"
    assert single["content"] == "beta"

    assert batch["success"] is True
    assert batch["mode"] == "batch"
    assert [item["content"] for item in batch["results"]] == ["alpha", "gamma"]


def test_server_read_text_can_include_line_numbers(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    first = tmp_path / "one.txt"
    first.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    single = _call(
        server.read_text,
        path=str(first),
        start_line=2,
        line_limit=2,
        include_line_numbers=True,
    )

    assert single["success"] is True
    assert single["mode"] == "single"
    assert single["content"] == "2: beta\n3: gamma"


def test_server_read_text_requires_exactly_one_path_argument(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    both_missing = _call(server.read_text)
    both_present = _call(server.read_text, path="one.txt", paths=["two.txt"])

    assert both_missing["success"] is False
    assert both_missing["error"]["code"] == "invalid_arguments"
    assert both_present["success"] is False
    assert both_present["error"]["code"] == "invalid_arguments"


def test_server_search_validates_mode_and_required_fields(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    invalid_mode = _call(server.search, mode="unknown", path=str(tmp_path))
    missing_regex_pattern = _call(server.search, mode="regex", path=str(tmp_path))
    missing_glob_pattern = _call(server.search, mode="glob", path=str(tmp_path))

    assert invalid_mode["success"] is False
    assert invalid_mode["error"]["code"] == "invalid_mode"
    assert missing_regex_pattern["success"] is False
    assert missing_regex_pattern["error"]["code"] == "missing_pattern"
    assert missing_glob_pattern["success"] is False
    assert missing_glob_pattern["error"]["code"] == "missing_pattern"


def test_server_search_supports_single_file_path_for_text_and_regex(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    target = tmp_path / "one.py"
    target.write_text("alpha\nTODO: fix me\n", encoding="utf-8")

    text_result = _call(server.search, mode="text", path=str(target), query="TODO")
    regex_result = _call(server.search, mode="regex", path=str(target), pattern=r"TODO:\s+\w+")

    assert text_result["success"] is True
    assert text_result["mode"] == "text"
    assert len(text_result["matches"]) == 1
    assert text_result["matches"][0]["path"] == str(target)

    assert regex_result["success"] is True
    assert regex_result["mode"] == "regex"
    assert len(regex_result["matches"]) == 1
    assert regex_result["matches"][0]["path"] == str(target)


def test_server_delegate_task_accepts_structured_fields(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    server.registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command="python3 -c \"print('codex')\"",
        claude_command="python3 -c \"print('claude')\"",
    )

    queued = _call(
        server.delegate_task,
        task="Implement the fallback flow",
        goal="Ship a working fallback task runner",
        cwd=str(tmp_path),
        acceptance_criteria=["Tool returns structured status"],
        verification_commands=["pytest -q"],
        commit_mode="allowed",
    )
    meta = _call(server.get_task, queued["task_id"])

    assert meta["goal"] == "Ship a working fallback task runner"
    assert meta["acceptance_criteria"] == ["Tool returns structured status"]
    assert meta["verification_commands"] == ["pytest -q"]
    assert meta["commit_mode"] == "allowed"


def test_server_run_command_stream_returns_task_polling_hint(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    old_registry = server.registry
    try:
        server.registry = ExecutorRegistry(
            store=TaskStore(tmp_path / "state"),
            codex_command="python3 -c \"print('codex')\"",
            claude_command="python3 -c \"print('claude')\"",
        )

        queued = _call(
            server.run_command_stream,
            command="python3 -c \"print('stream')\"",
            cwd=str(tmp_path),
            timeout=5,
        )
        result = _call(server.wait_task, queued["task_id"], timeout=2, poll_interval=0.05)

        assert queued["stream_mode"] == "task-polling"
        assert "next" in queued
        assert result["status"] == "succeeded"
        assert "stream" in result["stdout_tail"]
    finally:
        server.registry = old_registry


def test_server_purge_tasks_dry_run_reports_candidates(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    old_store = server.store
    old_registry = server.registry
    try:
        server.store = TaskStore(tmp_path / "state")
        server.registry = ExecutorRegistry(
            store=server.store,
            codex_command="python3 -c \"print('codex')\"",
            claude_command="python3 -c \"print('claude')\"",
        )

        created = server.store.create(task="old", executor="shell", cwd=str(tmp_path))
        meta_path = server.store._task_dir(created["task_id"]) / "meta.json"  # noqa: SLF001
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        payload["updated_at"] = "2000-01-01T00:00:00+00:00"
        meta_path.write_text(json.dumps(payload), encoding="utf-8")

        result = _call(server.purge_tasks, older_than_hours=1, dry_run=True)

        assert result["success"] is True
        assert result["purged"] == 1
        assert created["task_id"] in result["task_ids"]
    finally:
        server.store = old_store
        server.registry = old_registry


def test_server_apply_patch_tool_description_uses_generic_patch_language() -> None:
    from notion_local_ops_mcp import server

    async def scenario() -> str:
        list_tools = getattr(server.mcp, "_list_tools")
        try:
            tools = await list_tools()
        except TypeError:
            tools = await list_tools(None)
        apply_patch_tool = next(tool for tool in tools if tool.name == "apply_patch")
        return apply_patch_tool.description

    description = asyncio.run(scenario())

    assert "codex-style" not in description
    assert "*** Begin Patch" in description


def test_server_tools_expose_chatgpt_compatible_annotations() -> None:
    from notion_local_ops_mcp import server

    async def scenario() -> dict[str, dict[str, object]]:
        list_tools = getattr(server.mcp, "_list_tools")
        try:
            tools = await list_tools()
        except TypeError:
            tools = await list_tools(None)
        return {
            tool.name: {
                "title": tool.title,
                "annotations": tool.annotations.model_dump(exclude_none=True),
            }
            for tool in tools
        }

    descriptors = asyncio.run(scenario())
    annotations = {name: value["annotations"] for name, value in descriptors.items()}

    assert annotations
    assert all(value["title"] for value in descriptors.values())
    assert all(value for value in annotations.values())
    assert annotations["server_info"]["readOnlyHint"] is True
    assert annotations["search"]["readOnlyHint"] is True
    assert annotations["write_file"]["readOnlyHint"] is False
    assert annotations["write_file"]["destructiveHint"] is True
    assert annotations["run_command"]["openWorldHint"] is True
    assert annotations["delegate_task"]["openWorldHint"] is True
