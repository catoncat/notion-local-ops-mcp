from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific launcher smoke test")
def test_run_mcp_instance_script_executes_server_and_writes_log(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "run-mcp-instance.ps1"
    fake_server = tmp_path / "fake-server.cmd"
    log_path = tmp_path / "instance.log"
    state_dir = tmp_path / "state"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    fake_server.write_text(
        "@echo off\r\necho fake-server-started\r\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-ServerExecutable",
            str(fake_server),
            "-Port",
            "8766",
            "-WorkspaceRoot",
            str(workspace_root),
            "-StateDir",
            str(state_dir),
            "-LogPath",
            str(log_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert state_dir.is_dir()
    log_text = log_path.read_text(encoding="utf-8")
    assert "Starting MCP instance" in log_text
    assert "fake-server-started" in log_text
