from __future__ import annotations

import asyncio

from notion_local_ops_mcp.server import server_info


def _call() -> dict:
    fn = server_info.fn if hasattr(server_info, "fn") else server_info
    return asyncio.run(fn())


def test_server_info_reports_metadata_and_tools() -> None:
    payload = _call()
    assert payload["success"] is True
    assert payload["app_name"] == "notion-local-ops-mcp"
    assert isinstance(payload["port"], int)
    assert isinstance(payload["workspace_root"], str)
    assert payload["auth"] in {"none", "shared_token", "oauth"}
    assert payload["command_timeout_seconds"] >= 1
    assert payload["delegate_timeout_seconds"] >= 1
    tools = payload["tools"]
    assert isinstance(tools, list)
    # Spot-check a handful of must-have tools from each module.
    for name in [
        "server_info",
        "search",
        "read_text",
        "run_command",
        "apply_patch",
        "git_status",
        "git_show",
        "git_blame",
        "delegate_task",
        "run_command_stream",
        "purge_tasks",
    ]:
        assert name in tools, f"expected {name} in tools list"
    for removed in ["search_files", "glob_files", "grep_files", "read_file", "read_files", "replace_in_file"]:
        assert removed not in tools, f"did not expect legacy alias tool {removed}"
    assert payload["tool_count"] == len(tools)
