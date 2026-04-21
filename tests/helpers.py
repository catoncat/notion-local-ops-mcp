from __future__ import annotations

import json
import sys
from pathlib import Path


PYTHON_EXE = Path(sys.executable).resolve().as_posix()


def _escape_for_double_quotes(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def python_cmd(code: str) -> str:
    escaped = _escape_for_double_quotes(code)
    return f'"{PYTHON_EXE}" -c "{escaped}"'


def python_print_cmd(text: str) -> str:
    return python_cmd(f"print({text!r})")


def python_sleep_cmd(seconds: float, *, before: str | None = None) -> str:
    statements: list[str] = []
    if before is not None:
        statements.append(f"print({before!r})")
    statements.append("import time")
    statements.append(f"time.sleep({seconds!r})")
    return python_cmd("; ".join(statements))


def python_json_cmd(payload: object) -> str:
    return python_cmd(f"print({json.dumps(payload)!r})")


def normalize_path_slashes(value: str) -> str:
    return value.replace("\\", "/")
