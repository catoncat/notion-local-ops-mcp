from __future__ import annotations

import subprocess
from pathlib import Path


# Sentinel exit code used when the process did not produce a real return code
# (e.g. it timed out or never started). Keeping this as an int (not None) so
# callers can always do numeric comparisons like `exit_code == 0` without
# special-casing `None`.
TIMEOUT_EXIT_CODE = -1


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def run_command(*, command: str, cwd: Path, timeout: int) -> dict[str, object]:
    if not cwd.exists():
        return {
            "success": False,
            "error": {
                "code": "cwd_not_found",
                "message": f"Working directory not found: {cwd}",
            },
            "cwd": str(cwd),
            "command": command,
            "exit_code": TIMEOUT_EXIT_CODE,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
        }
    if not cwd.is_dir():
        return {
            "success": False,
            "error": {
                "code": "cwd_not_directory",
                "message": f"Working directory is not a directory: {cwd}",
            },
            "cwd": str(cwd),
            "command": command,
            "exit_code": TIMEOUT_EXIT_CODE,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
        }

    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return {
            "success": completed.returncode == 0,
            "command": command,
            "cwd": str(cwd),
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "command": command,
            "cwd": str(cwd),
            "exit_code": TIMEOUT_EXIT_CODE,
            "stdout": _decode_output(exc.stdout),
            "stderr": _decode_output(exc.stderr),
            "timed_out": True,
            "timeout": timeout,
            "error": {
                "code": "timed_out",
                "message": (
                    f"Command exceeded the {timeout}s timeout. "
                    "Retry with a larger `timeout` argument, or set "
                    "`run_in_background=true` to queue it as a task and poll "
                    "with wait_task/get_task."
                ),
            },
            "hint": "consider_delegate_task",
        }
