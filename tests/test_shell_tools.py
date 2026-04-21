from pathlib import Path

from notion_local_ops_mcp.shell import TIMEOUT_EXIT_CODE, run_command
from tests.helpers import python_print_cmd, python_sleep_cmd


def test_run_command_returns_stdout_and_exit_code(tmp_path: Path) -> None:
    result = run_command(
        command=python_print_cmd("hello"),
        cwd=tmp_path,
        timeout=5,
    )

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hello"
    assert result["timed_out"] is False


def test_run_command_timeout_returns_unified_shape(tmp_path: Path) -> None:
    result = run_command(
        command=python_sleep_cmd(2),
        cwd=tmp_path,
        timeout=1,
    )

    assert result["success"] is False
    assert result["timed_out"] is True
    # exit_code is always an int, never None, so callers can do numeric compares.
    assert isinstance(result["exit_code"], int)
    assert result["exit_code"] == TIMEOUT_EXIT_CODE
    assert result["timeout"] == 1
    assert result["error"]["code"] == "timed_out"
    assert "timeout" in result["error"]["message"].lower()
    assert result["hint"] == "consider_delegate_task"


def test_run_command_cwd_errors_include_exit_code_field(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result = run_command(command="echo hi", cwd=missing, timeout=5)

    assert result["success"] is False
    assert result["timed_out"] is False
    # Even error shapes carry exit_code / stdout / stderr so LLM handling is uniform.
    assert isinstance(result["exit_code"], int)
    assert result["stdout"] == ""
    assert result["stderr"] == ""
    assert result["error"]["code"] == "cwd_not_found"
