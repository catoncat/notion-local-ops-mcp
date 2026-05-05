from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import textwrap
import time

import pytest


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _find_free_port_base_away_from(blocked_ports: set[int], *, span: int = 8) -> int:
    while True:
        port_base = _find_free_port()
        if port_base + span >= 65535:
            continue
        if any(port in blocked_ports for port in range(port_base, port_base + span)):
            continue

        sockets: list[socket.socket] = []
        try:
            for port in range(port_base, port_base + span):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    sock.close()
                    raise
                sockets.append(sock)
        except OSError:
            continue
        finally:
            for sock in sockets:
                sock.close()

        return port_base


def _wait_for(predicate, *, timeout: float = 20.0, interval: float = 0.2):
    deadline = time.time() + timeout
    last_value = None
    while time.time() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(interval)
    raise AssertionError(f"Timed out after {timeout} seconds waiting for condition. Last value: {last_value!r}")


def _read_json(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return None


def _kill_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        check=False,
    )


def _cleanup_launcher_state(launcher_state_path: Path) -> None:
    state = _read_json(launcher_state_path)
    if not state:
        return

    for item in state.get("instances", []):
        if not isinstance(item, dict):
            continue
        _kill_process_tree(int(item.get("cloudflared_pid") or 0))
        _kill_process_tree(int(item.get("server_pid") or 0))


def _write_fake_cloudflared(
    tmp_path: Path,
    *,
    exit_first_after_seconds: float = 0.0,
    port_base: int | None = None,
) -> tuple[Path, Path, int]:
    state_path = tmp_path / "fake-cloudflared-state.json"
    script_path = tmp_path / "fake_cloudflared.py"
    if port_base is None:
        port_base = _find_free_port()

    script_path.write_text(
        textwrap.dedent(
            """
            import http.server
            import json
            import os
            import pathlib
            import signal
            import sys
            import threading
            import time

            state_path = pathlib.Path(os.environ["FAKE_CLOUDFLARED_STATE"])
            port_base = int(os.environ["FAKE_CLOUDFLARED_PORT_BASE"])
            exit_after = float(os.environ.get("FAKE_CLOUDFLARED_EXIT_AFTER_SECONDS", "0"))

            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
            else:
                state = {"count": 0}

            state["count"] += 1
            count = state["count"]
            state_path.write_text(json.dumps(state), encoding="utf-8")

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok": true}')

                def log_message(self, fmt, *args):
                    return

            requested_port = port_base + count - 1
            try:
                server = http.server.ThreadingHTTPServer(("127.0.0.1", requested_port), Handler)
            except OSError:
                server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            port = int(server.server_address[1])
            stop_event = threading.Event()

            def _stop(*_args):
                stop_event.set()
                try:
                    server.shutdown()
                except Exception:
                    pass

            for signal_name in ("SIGTERM", "SIGINT"):
                if hasattr(signal, signal_name):
                    signal.signal(getattr(signal, signal_name), _stop)

            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            thread.start()

            url = f"http://127.0.0.1:{port}"
            sys.stderr.write(url + "\\n")
            sys.stderr.flush()

            try:
                if count == 1 and exit_after > 0:
                    time.sleep(exit_after)
                else:
                    while not stop_event.is_set():
                        time.sleep(0.2)
            finally:
                try:
                    server.shutdown()
                except Exception:
                    pass
                server.server_close()
            """
        ),
        encoding="utf-8",
    )

    script_path.chmod(0o755)
    return script_path, state_path, port_base


def _launcher_env(
    repo_root: Path,
    tmp_path: Path,
    fake_cloudflared: Path,
    fake_cloudflared_state_path: Path,
    fake_cloudflared_port_base: int,
    fake_cloudflared_exit_after_seconds: float,
    base_port: int,
    *,
    extra_env: dict[str, str] | None = None,
) -> tuple[dict[str, str], Path, Path]:
    launcher_state_dir = tmp_path / "launcher-state"
    launcher_state_path = launcher_state_dir / "active-instances.json"
    status_path = tmp_path / "Notion-MCP-status.txt"

    env = os.environ.copy()
    env.update(
        {
            "NOTION_LOCAL_OPS_HOST": "127.0.0.1",
            "NOTION_LOCAL_OPS_PORT": str(base_port),
            "NOTION_LOCAL_OPS_WORKSPACE_ROOT": str(repo_root),
            "NOTION_LOCAL_OPS_STATE_DIR": str(tmp_path / "instance-state"),
            "NOTION_LOCAL_OPS_LAUNCHER_STATE_DIR": str(launcher_state_dir),
            "NOTION_LOCAL_OPS_STATUS_PATH": str(status_path),
            "NOTION_LOCAL_OPS_AUTH_TOKEN": "test-token",
            "NOTION_LOCAL_OPS_AUTH_TOKEN_SECOND": "test-token",
            "NOTION_LOCAL_OPS_CLOUDFLARED_COMMAND": str(fake_cloudflared),
            "NOTION_LOCAL_OPS_TEST_PUBLIC_PROBE_TIMEOUT_SECONDS": "1",
            "FAKE_CLOUDFLARED_STATE": str(fake_cloudflared_state_path),
            "FAKE_CLOUDFLARED_PORT_BASE": str(fake_cloudflared_port_base),
            "FAKE_CLOUDFLARED_EXIT_AFTER_SECONDS": str(fake_cloudflared_exit_after_seconds),
        }
    )
    if extra_env:
        env.update(extra_env)

    return env, launcher_state_path, status_path


def _start_launcher(
    repo_root: Path,
    env: dict[str, str],
    *,
    base_port: int,
    requested_count: int = 1,
    monitor_cycles: int = 8,
    monitor_interval_seconds: int = 1,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repo_root / "scripts" / "launch-mcp-manager.ps1"),
            "-RequestedCount",
            str(requested_count),
            "-RequestedBasePort",
            str(base_port),
            "-NonInteractive",
            "-MonitorCycles",
            str(monitor_cycles),
            "-MonitorIntervalSeconds",
            str(monitor_interval_seconds),
        ],
        cwd=repo_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_launcher_instance(launcher_state_path: Path) -> dict[str, object]:
    def _get_first_instance() -> dict[str, object] | None:
        state = _read_json(launcher_state_path)
        if not state:
            return None
        instances = state.get("instances", [])
        if not isinstance(instances, list) or not instances:
            return None
        first = instances[0]
        if not isinstance(first, dict):
            return None
        public_mcp_url = first.get("public_mcp_url") or first.get("mcp_url")
        if not public_mcp_url:
            return None
        return first

    return _wait_for(_get_first_instance)


def _wait_for_launcher_state(
    launcher_state_path: Path,
    predicate,
    *,
    timeout: float = 60.0,
    interval: float = 0.5,
) -> dict[str, object]:
    def _get_matching_instance() -> dict[str, object] | None:
        state = _read_json(launcher_state_path)
        if not state:
            return None
        instances = state.get("instances", [])
        if not isinstance(instances, list) or not instances:
            return None
        first = instances[0]
        if not isinstance(first, dict):
            return None
        if predicate(first):
            return first
        return None

    return _wait_for(_get_matching_instance, timeout=timeout, interval=interval)


def _wait_for_launcher_instances(
    launcher_state_path: Path,
    *,
    expected_count: int,
    timeout: float = 40.0,
) -> dict[str, object]:
    def _get_ready_state() -> dict[str, object] | None:
        state = _read_json(launcher_state_path)
        if not state:
            return None
        instances = state.get("instances", [])
        if not isinstance(instances, list) or len(instances) != expected_count:
            return None
        for item in instances:
            if not isinstance(item, dict):
                return None
            if not (item.get("public_mcp_url") or item.get("mcp_url")):
                return None
        return state

    return _wait_for(_get_ready_state, timeout=timeout, interval=0.5)


def _process_exists(pid: int) -> bool:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-Command",
            f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return completed.returncode == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific launcher smoke tests")
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
    assert "MCP instance exited with code 0" in log_text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific launcher smoke tests")
def test_launch_manager_rebuilds_quick_tunnel_and_preserves_connection_name(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    base_port = _find_free_port()
    fake_cloudflared_port_base = _find_free_port_base_away_from({base_port})
    fake_cloudflared, fake_cloudflared_state_path, fake_cloudflared_port_base = _write_fake_cloudflared(
        tmp_path,
        exit_first_after_seconds=1.2,
        port_base=fake_cloudflared_port_base,
    )
    env, launcher_state_path, status_path = _launcher_env(
        repo_root,
        tmp_path,
        fake_cloudflared,
        fake_cloudflared_state_path,
        fake_cloudflared_port_base,
        1.2,
        base_port,
    )

    process = _start_launcher(repo_root, env, base_port=base_port, monitor_cycles=10, monitor_interval_seconds=1)
    try:
        initial_instance = _wait_for_launcher_instance(launcher_state_path)
        initial_server_pid = int(initial_instance["server_pid"])
        initial_public_mcp_url = str(initial_instance["public_mcp_url"])
        initial_name = str(initial_instance["name"])

        final_instance = _wait_for_launcher_state(
            launcher_state_path,
            lambda item: (
                int(item.get("restart_count") or 0) >= 1
                and str(item.get("public_mcp_url") or "") != initial_public_mcp_url
                and int(item.get("server_pid") or 0) == initial_server_pid
                and bool(item.get("needs_notion_url_update")) is True
            ),
        )
        assert final_instance["public_mcp_url"] != initial_public_mcp_url
        assert int(final_instance["server_pid"]) == initial_server_pid
        assert int(final_instance["restart_count"]) >= 1
        assert final_instance["tunnel_mode"] == "quick"
        assert bool(final_instance["needs_notion_url_update"]) is True
        # Connection name must stay the same across quick tunnel rebuilds
        assert str(final_instance["name"]) == initial_name
        # mcp_url must equal public_mcp_url (the URL Notion connects to)
        assert str(final_instance["mcp_url"]) == str(final_instance["public_mcp_url"])

        status_text = _wait_for(
            lambda: (
                status_path.read_text(encoding="utf-8")
                if status_path.exists()
                and "Public MCP URL changed" in status_path.read_text(encoding="utf-8")
                and str(final_instance["public_mcp_url"]) in status_path.read_text(encoding="utf-8")
                else ""
            ),
            timeout=10.0,
        )
        assert "Public MCP URL changed" in status_text
        assert str(final_instance["public_mcp_url"]) in status_text
        # Status text must explicitly tell the user to keep the same connection name
        assert "Keep the same Notion Agent connection name" in status_text
        assert "update only the connector URL" in status_text
    finally:
        _kill_process_tree(process.pid)
        _cleanup_launcher_state(launcher_state_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific launcher smoke tests")
def test_launch_manager_restarts_server_and_tunnel_when_server_dies(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    base_port = _find_free_port()
    fake_cloudflared_port_base = _find_free_port_base_away_from({base_port})
    fake_cloudflared, fake_cloudflared_state_path, fake_cloudflared_port_base = _write_fake_cloudflared(
        tmp_path,
        exit_first_after_seconds=0.0,
        port_base=fake_cloudflared_port_base,
    )
    env, launcher_state_path, _ = _launcher_env(
        repo_root,
        tmp_path,
        fake_cloudflared,
        fake_cloudflared_state_path,
        fake_cloudflared_port_base,
        0.0,
        base_port,
    )

    process = _start_launcher(repo_root, env, base_port=base_port, monitor_cycles=10, monitor_interval_seconds=1)
    try:
        initial_instance = _wait_for_launcher_instance(launcher_state_path)
        initial_server_pid = int(initial_instance["server_pid"])
        initial_tunnel_pid = int(initial_instance["cloudflared_pid"])
        initial_public_mcp_url = str(initial_instance["public_mcp_url"])

        _kill_process_tree(initial_server_pid)

        final_instance = _wait_for_launcher_state(
            launcher_state_path,
            lambda item: (
                int(item.get("restart_count") or 0) >= 1
                and int(item.get("server_pid") or 0) != initial_server_pid
                and int(item.get("cloudflared_pid") or 0) != initial_tunnel_pid
                and str(item.get("public_mcp_url") or "") != initial_public_mcp_url
            ),
        )
        assert int(final_instance["restart_count"]) >= 1
        assert int(final_instance["server_pid"]) != initial_server_pid
        assert int(final_instance["cloudflared_pid"]) != initial_tunnel_pid
        assert str(final_instance["public_mcp_url"]) != initial_public_mcp_url
        assert "Local instance unhealthy" in str(final_instance["last_failure_reason"] or "") or final_instance["last_failure_reason"] == ""
    finally:
        _kill_process_tree(process.pid)
        _cleanup_launcher_state(launcher_state_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific launcher smoke tests")
def test_stop_script_closes_all_open_mcp_instances(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    base_port = _find_free_port()
    fake_cloudflared_port_base = _find_free_port_base_away_from({base_port, base_port + 1})
    fake_cloudflared, fake_cloudflared_state_path, fake_cloudflared_port_base = _write_fake_cloudflared(
        tmp_path,
        exit_first_after_seconds=0.0,
        port_base=fake_cloudflared_port_base,
    )
    env, launcher_state_path, status_path = _launcher_env(
        repo_root,
        tmp_path,
        fake_cloudflared,
        fake_cloudflared_state_path,
        fake_cloudflared_port_base,
        0.0,
        base_port,
    )

    process = _start_launcher(
        repo_root,
        env,
        base_port=base_port,
        requested_count=2,
        monitor_cycles=0,
        monitor_interval_seconds=1,
    )
    try:
        state = _wait_for_launcher_instances(launcher_state_path, expected_count=2)
        instances = state["instances"]
        pids = {
            int(item["server_pid"])
            for item in instances
            if isinstance(item, dict) and item.get("server_pid")
        } | {
            int(item["cloudflared_pid"])
            for item in instances
            if isinstance(item, dict) and item.get("cloudflared_pid")
        }

        stop_completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(repo_root / "scripts" / "stop-mcp-manager.ps1"),
            ],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert stop_completed.returncode == 0, stop_completed.stderr or stop_completed.stdout
        process.wait(timeout=60)
        assert process.returncode is not None
        assert not launcher_state_path.exists()

        status_text = status_path.read_text(encoding="utf-8")
        assert "Notion Local MCP launcher stopped." in status_text

        assert _wait_for(lambda: all(not _process_exists(pid) for pid in pids), timeout=30.0)
    finally:
        _kill_process_tree(process.pid)
        _cleanup_launcher_state(launcher_state_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific launcher smoke tests")
def test_launch_manager_fails_fast_when_venv_is_missing(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    fake_repo = tmp_path / "fake-repo"
    (fake_repo / "scripts").mkdir(parents=True)
    shutil.copy2(repo_root / "scripts" / "launch-mcp-manager.ps1", fake_repo / "scripts" / "launch-mcp-manager.ps1")
    shutil.copy2(repo_root / "scripts" / "run-mcp-instance.ps1", fake_repo / "scripts" / "run-mcp-instance.ps1")
    shutil.copy2(repo_root / "pyproject.toml", fake_repo / "pyproject.toml")

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(fake_repo / "scripts" / "launch-mcp-manager.ps1"),
            "-RequestedCount",
            "1",
            "-RequestedBasePort",
            str(_find_free_port()),
            "-NonInteractive",
            "-MonitorCycles",
            "1",
            "-MonitorIntervalSeconds",
            "1",
        ],
        cwd=fake_repo,
        capture_output=True,
        text=True,
        timeout=30,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "Missing Python runtime" in output
    assert '.\\.venv\\Scripts\\python.exe -m pip install -e ".[dev]"' in output
    assert not (fake_repo / ".state" / "launcher" / "active-instances.json").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific launcher smoke tests")
def test_launch_manager_fails_fast_on_fastmcp_version_drift(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    fake_cloudflared, fake_cloudflared_state_path, fake_cloudflared_port_base = _write_fake_cloudflared(
        tmp_path,
        exit_first_after_seconds=0.0,
    )
    base_port = _find_free_port()
    env, launcher_state_path, _ = _launcher_env(
        repo_root,
        tmp_path,
        fake_cloudflared,
        fake_cloudflared_state_path,
        fake_cloudflared_port_base,
        0.0,
        base_port,
        extra_env={"NOTION_LOCAL_OPS_TEST_FORCE_FASTMCP_VERSION": "3.2.3"},
    )

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repo_root / "scripts" / "launch-mcp-manager.ps1"),
            "-RequestedCount",
            "1",
            "-RequestedBasePort",
            str(base_port),
            "-NonInteractive",
            "-MonitorCycles",
            "1",
            "-MonitorIntervalSeconds",
            "1",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "outside supported range" in output
    assert '.\\.venv\\Scripts\\python.exe -m pip install -e ".[dev]"' in output
    assert not launcher_state_path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific launcher smoke tests")
def test_launch_manager_fails_fast_on_fastmcp_metadata_drift(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    fake_cloudflared, fake_cloudflared_state_path, fake_cloudflared_port_base = _write_fake_cloudflared(
        tmp_path,
        exit_first_after_seconds=0.0,
    )
    base_port = _find_free_port()
    env, launcher_state_path, _ = _launcher_env(
        repo_root,
        tmp_path,
        fake_cloudflared,
        fake_cloudflared_state_path,
        fake_cloudflared_port_base,
        0.0,
        base_port,
        extra_env={"NOTION_LOCAL_OPS_TEST_FORCE_FASTMCP_METADATA_SPEC": ">=2.12.0,<3"},
    )

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repo_root / "scripts" / "launch-mcp-manager.ps1"),
            "-RequestedCount",
            "1",
            "-RequestedBasePort",
            str(base_port),
            "-NonInteractive",
            "-MonitorCycles",
            "1",
            "-MonitorIntervalSeconds",
            "1",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "installed editable metadata has stale fastmcp requirement" in output
    assert '.\\.venv\\Scripts\\python.exe -m pip install -e ".[dev]"' in output
    assert not launcher_state_path.exists()
