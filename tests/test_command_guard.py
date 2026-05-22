from __future__ import annotations

from notion_local_ops_mcp.command_guard import evaluate_command


def test_command_guard_defaults_to_off() -> None:
    decision = evaluate_command("curl https://example.com", mode="off")

    assert decision.allowed is True
    assert decision.warnings == []


def test_command_guard_warn_mode_allows_with_warning() -> None:
    decision = evaluate_command("curl https://example.com", mode="warn")

    assert decision.allowed is True
    assert decision.warnings
    assert decision.warnings[0]["code"] == "network_command"


def test_command_guard_block_mode_denies_risky_command() -> None:
    decision = evaluate_command("rm -rf build", mode="block")

    assert decision.allowed is False
    assert decision.warnings[0]["code"] == "destructive_command"
