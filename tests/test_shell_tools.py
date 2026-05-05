from pathlib import Path

import subprocess

from notion_local_ops_mcp.shell import TIMEOUT_EXIT_CODE, _decode_output, run_command
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


def test_decode_output_handles_bytes() -> None:
    """_decode_output should decode bytes to string."""
    assert _decode_output("中文".encode("utf-8")) == "中文"
    assert _decode_output(b"err") == "err"


def test_decode_output_handles_string() -> None:
    assert _decode_output("hello") == "hello"


def test_decode_output_handles_none() -> None:
    assert _decode_output(None) == ""


def test_run_command_timeout_decodes_bytes(monkeypatch, tmp_path: Path) -> None:
    """When TimeoutExpired contains bytes stdout/stderr, run_command should
    decode them to strings, not leak raw bytes into the JSON response."""
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd="x",
            timeout=1,
            output="中文".encode("utf-8"),
            stderr=b"err",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_command(command="x", cwd=tmp_path, timeout=1)
    assert isinstance(result["stdout"], str)
    assert result["stdout"] == "中文"
    assert isinstance(result["stderr"], str)
    assert result["stderr"] == "err"
