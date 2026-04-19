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
    assert payload["auth"] in {"bearer", "none"}
    assert payload["command_timeout_seconds"] >= 1
    assert payload["delegate_timeout_seconds"] >= 1
    tools = payload["tools"]
    assert isinstance(tools, list)
    # Spot-check a handful of must-have tools from each module.
    for name in [
        "server_info",
        "run_command",
        "read_file",
        "replace_in_file",
        "apply_patch",
        "git_status",
        "git_show",
        "git_blame",
        "delegate_task",
    ]:
        assert name in tools, f"expected {name} in tools list"
    assert payload["tool_count"] == len(tools)
