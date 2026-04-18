from pathlib import Path

from notion_local_ops_mcp.shell import run_command
from tests.helpers import python_command


def test_run_command_returns_stdout_and_exit_code(tmp_path: Path) -> None:
    result = run_command(
        command=python_command("print('hello')"),
        cwd=tmp_path,
        timeout=5,
    )

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hello"
    assert result["timed_out"] is False
