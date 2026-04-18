from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


def python_command(code: str) -> str:
    args = [sys.executable, "-c", code]
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def init_git_repo(path: Path) -> None:
    run_git(path, "init")
    run_git(path, "config", "user.name", "Test User")
    run_git(path, "config", "user.email", "test@example.com")


def commit_file(path: Path, filename: str, content: str, message: str) -> str:
    target = path / filename
    target.write_text(content, encoding="utf-8")
    run_git(path, "add", filename)
    run_git(path, "commit", "-m", message)
    return run_git(path, "rev-parse", "HEAD").stdout.strip()
