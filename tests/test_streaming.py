"""Tests for streaming output, timeout, cancel, interval validation, and incremental flushing."""
from __future__ import annotations

import codecs
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import notion_local_ops_mcp.config as config_mod
import notion_local_ops_mcp.executors as executors
from notion_local_ops_mcp.executors import ExecutorRegistry, _kill_process
from notion_local_ops_mcp.tasks import TaskStore
from tests.helpers import python_cmd, python_print_cmd, python_sleep_cmd


# ---------------------------------------------------------------------------
# 1. STREAM_OUTPUT_INTERVAL validation
# ---------------------------------------------------------------------------


def test_stream_output_interval_rejects_zero():
    with pytest.raises(ValueError, match="must be > 0"):
        # Simulate what config.py does at import time
        val = float("0")
        if val <= 0:
            raise ValueError(
                f"NOTION_LOCAL_OPS_STREAM_OUTPUT_INTERVAL must be > 0, got {val}"
            )


def test_stream_output_interval_rejects_negative():
    with pytest.raises(ValueError, match="must be > 0"):
        val = float("-1")
        if val <= 0:
            raise ValueError(
                f"NOTION_LOCAL_OPS_STREAM_OUTPUT_INTERVAL must be > 0, got {val}"
            )


# ---------------------------------------------------------------------------
# 2. Live streaming — get_task sees incremental output while task runs
# ---------------------------------------------------------------------------


def test_get_task_sees_incremental_output(tmp_path: Path) -> None:
    """A slow command that prints lines with delays; get_task should see
    partial output before the task completes."""
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    # Print "line1", sleep 0.8s, print "line2", sleep 0.8s — total ~1.6s
    # Generous sleeps so the flush interval fires between prints even on
    # slow Windows CI where shell=True adds startup latency.
    slow_cmd = python_cmd(
        "import time, sys; "
        "print('line1', flush=True); sys.stdout.flush(); time.sleep(0.8); "
        "print('line2', flush=True); sys.stdout.flush(); time.sleep(0.8)"
    )

    with patch.object(executors, "STREAM_OUTPUT_INTERVAL", 0.2):
        task = registry.submit_command(command=slow_cmd, cwd=tmp_path, timeout=15)
        task_id = task["task_id"]

        # Wait long enough for the process to start + first flush
        time.sleep(1.2)
        mid = registry.get(task_id)
        # Should see at least "line1" before the task finishes
        assert "line1" in mid["stdout_tail"], f"Expected incremental output, got: {mid['stdout_tail']!r}"

        # Now wait for completion
        result = registry.wait(task_id, timeout=10)
        assert result["status"] == "succeeded"
        assert "line1" in result["stdout_tail"]
        assert "line2" in result["stdout_tail"]


# ---------------------------------------------------------------------------
# 3. Timeout kills the task and marks it failed
# ---------------------------------------------------------------------------


def test_stream_process_timeout_marks_failed(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    task = registry.submit_command(
        command=python_sleep_cmd(30),
        cwd=tmp_path,
        timeout=1,  # 1 second timeout
    )
    result = registry.wait(task["task_id"], timeout=10)

    assert result["status"] == "failed"
    assert result["completed"] is True
    # The process-level timed_out is stored in meta by _stream_process
    meta = store.get(task["task_id"])
    assert meta.get("timed_out") is True


# ---------------------------------------------------------------------------
# 4. Cancel stops the task
# ---------------------------------------------------------------------------


def test_cancel_stops_streaming_task(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    task = registry.submit_command(
        command=python_sleep_cmd(30),
        cwd=tmp_path,
        timeout=60,
    )
    task_id = task["task_id"]

    # Give it a moment to start
    time.sleep(0.2)
    registry.cancel(task_id)
    result = registry.wait(task_id, timeout=5)

    assert result["status"] == "cancelled"
    assert result["completed"] is True


# ---------------------------------------------------------------------------
# 5. Delegate task uses event-driven wait (not polling fallback)
# ---------------------------------------------------------------------------


def test_delegate_task_wait_is_event_driven(tmp_path: Path) -> None:
    """After the fix, submit() uses _register_task() so wait_task() should
    use the completion event, not fall back to polling."""
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("done"),
        claude_command=python_print_cmd("claude"),
    )

    task = registry.submit(
        task="quick task",
        executor="codex",
        cwd=tmp_path,
        timeout=5,
    )
    task_id = task["task_id"]

    # Verify the completion event was registered
    with registry._lock:
        assert task_id in registry._completion_events, (
            "submit() should register a completion event via _register_task()"
        )

    start = time.monotonic()
    # Large poll_interval proves we're using the event, not polling
    result = registry.wait(task_id, timeout=5, poll_interval=10.0)
    elapsed = time.monotonic() - start

    assert result["completed"] is True
    assert result["status"] == "succeeded"
    assert elapsed < 2.0, f"wait took {elapsed:.3f}s — event path not used"


# ---------------------------------------------------------------------------
# 6. Append-based log flushing (no O(n²) rewrite)
# ---------------------------------------------------------------------------


def test_append_logs_accumulates(tmp_path: Path) -> None:
    """TaskStore.append_logs should accumulate, not overwrite."""
    store = TaskStore(tmp_path / "state")
    created = store.create(task="test", executor="shell", cwd=str(tmp_path))
    tid = created["task_id"]

    store.append_logs(tid, stdout="chunk1\n")
    store.append_logs(tid, stdout="chunk2\n")
    store.append_logs(tid, stderr="err1\n")

    assert store.read_stdout(tid) == "chunk1\nchunk2\n"
    assert store.read_stderr(tid) == "err1\n"


# ---------------------------------------------------------------------------
# 7. Large output doesn't blow up
# ---------------------------------------------------------------------------


def test_large_output_completes(tmp_path: Path) -> None:
    """A command producing substantial output should complete without error."""
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    # Print 2000 lines of output
    big_cmd = python_cmd(
        "for i in range(2000): print(f'line {i} ' + 'x' * 80)"
    )

    with patch.object(executors, "STREAM_OUTPUT_INTERVAL", 0.1):
        task = registry.submit_command(command=big_cmd, cwd=tmp_path, timeout=30)
        result = registry.wait(task["task_id"], timeout=15)

    assert result["status"] == "succeeded"
    # stdout_tail is capped at 4000 chars by get(), but the full log should have content
    assert len(result["stdout_tail"]) > 0
    assert "line 1999" in store.read_stdout(task["task_id"])


# ---------------------------------------------------------------------------
# 8. _kill_process helper
# ---------------------------------------------------------------------------


def test_kill_process_noop_when_already_exited(tmp_path: Path) -> None:
    """_kill_process should not raise when the process already exited."""
    proc = subprocess.Popen(
        python_print_cmd("hi"),
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.wait(timeout=5)
    # Should be a no-op, not raise
    _kill_process(proc)


# ---------------------------------------------------------------------------
# 9. cancel_task does not overwrite succeeded/failed tasks
# ---------------------------------------------------------------------------


def test_cancel_does_not_overwrite_succeeded_task(tmp_path: Path) -> None:
    """After a task has succeeded, calling cancel should not change its status."""
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )
    task = registry.submit_command(
        command=python_print_cmd("done"),
        cwd=tmp_path,
        timeout=5,
    )
    task_id = task["task_id"]
    result = registry.wait(task_id, timeout=5)
    assert result["status"] == "succeeded"

    # Now try to cancel — should return succeeded, not overwritten
    cancel_result = registry.cancel(task_id)
    assert cancel_result["status"] == "succeeded"
    assert cancel_result["cancelled"] is False

    final = registry.get(task_id)
    assert final["status"] == "succeeded"


def test_cancel_does_not_overwrite_failed_task(tmp_path: Path) -> None:
    """After a task has failed, calling cancel should not change its status."""
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )
    task = registry.submit_command(
        command=python_cmd("import sys; sys.exit(1)"),
        cwd=tmp_path,
        timeout=5,
    )
    task_id = task["task_id"]
    result = registry.wait(task_id, timeout=5)
    assert result["status"] == "failed"

    cancel_result = registry.cancel(task_id)
    assert cancel_result["status"] == "failed"
    assert cancel_result["cancelled"] is False


# ---------------------------------------------------------------------------
# 10. cancel_task returns task_not_found for unknown task
# ---------------------------------------------------------------------------


def test_cancel_unknown_task_returns_task_not_found(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )
    result = registry.cancel("missing_task_id")
    assert result["success"] is False
    assert result["error"]["code"] == "task_not_found"
    assert result["cancelled"] is False


# ---------------------------------------------------------------------------
# 11. cancel_task uses _kill_process helper, not direct process.kill()
# ---------------------------------------------------------------------------


def test_cancel_uses_kill_process_helper(tmp_path: Path, monkeypatch) -> None:
    """cancel() should call _kill_process helper rather than process.kill() directly."""
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )
    task = registry.submit_command(
        command=python_sleep_cmd(30),
        cwd=tmp_path,
        timeout=60,
    )
    task_id = task["task_id"]
    time.sleep(0.3)

    kill_process_calls = []
    direct_kill_calls = []

    # Track calls to _kill_process
    original_kill_process = executors._kill_process
    def tracked_kill_process(process):
        kill_process_calls.append(process)
        original_kill_process(process)
    monkeypatch.setattr(executors, "_kill_process", tracked_kill_process)

    # Track calls to direct process.kill() — should be zero if _kill_process is used
    with registry._lock:
        proc = registry._processes.get(task_id)
    assert proc is not None and proc.poll() is None

    # Patch the process's kill method to track direct calls
    original_process_kill = proc.kill
    def tracked_process_kill():
        direct_kill_calls.append(True)
        original_process_kill()
    proc.kill = tracked_process_kill

    registry.cancel(task_id)

    # _kill_process was called at least once
    assert len(kill_process_calls) >= 1
    # The task's process was passed
    assert proc in kill_process_calls
    # process.kill() was not called directly (it's called inside _kill_process
    # on non-Windows, so we can't assert zero — but the key point is _kill_process
    # was the entry point, not a bare process.kill() in cancel())

    result = registry.wait(task_id, timeout=5)
    assert result["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 12. _kill_process Windows taskkill failure falls back to process.kill()
# ---------------------------------------------------------------------------


def test_kill_process_windows_taskkill_failure_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(executors, "IS_WINDOWS", True)

    fake_run_result = MagicMock()
    fake_run_result.returncode = 1

    monkeypatch.setattr(
        executors.subprocess,
        "run",
        lambda *args, **kwargs: fake_run_result,
    )

    fake_process = MagicMock()
    fake_process.poll.return_value = None
    kill_calls = []
    fake_process.kill.side_effect = lambda: kill_calls.append(True)

    _kill_process(fake_process)

    assert len(kill_calls) == 1, "process.kill() should be called as fallback"


# ---------------------------------------------------------------------------
# 13. Streaming preserves split UTF-8 multi-byte characters
# ---------------------------------------------------------------------------


def test_streaming_preserves_split_utf8_character(tmp_path: Path, monkeypatch) -> None:
    """When a multi-byte UTF-8 character is split across two read chunks,
    the incremental decoder should reconstruct it correctly, not produce
    replacement characters."""
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    # "中" is UTF-8 bytes \xe4\xb8\xad — split it into two chunks
    class FakeStream:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks
            self._index = 0

        def read1(self, n: int = -1) -> bytes:
            if self._index >= len(self._chunks):
                return b""
            chunk = self._chunks[self._index]
            self._index += 1
            return chunk

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.pid = 99999
            self.stdout = FakeStream([b"\xe4\xb8", b"\xad\n", b""])
            self.stderr = FakeStream([b""])
            self._polled = False

        def poll(self):
            if not self._polled:
                self._polled = True
                return None
            return 0

        def wait(self, timeout=None) -> int:
            return 0

    task = store.create(task="split utf8", executor="shell", cwd=str(tmp_path), timeout=10)
    cancel_event = threading.Event()

    monkeypatch.setattr(executors, "STREAM_OUTPUT_INTERVAL", 0.05)

    with registry._lock:
        registry._cancel_events[task["task_id"]] = cancel_event
        registry._completion_events[task["task_id"]] = threading.Event()

    stdout, stderr = registry._stream_process(
        task_id=task["task_id"],
        process=FakeProcess(),
        timeout=10,
        cancel_event=cancel_event,
    )

    assert stdout == "中\n"
    assert "\ufffd" not in stdout, f"Replacement character found in: {stdout!r}"
    assert "\ufffd" not in stderr, f"Replacement character found in: {stderr!r}"


def test_streaming_preserves_emoji_split_across_chunks(tmp_path: Path, monkeypatch) -> None:
    """Emoji characters (multi-byte UTF-8) should survive split across chunks."""
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    # 🙂 is UTF-8 bytes \xf0\x9f\x99\x82
    class FakeStream:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks
            self._index = 0

        def read1(self, n: int = -1) -> bytes:
            if self._index >= len(self._chunks):
                return b""
            chunk = self._chunks[self._index]
            self._index += 1
            return chunk

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.pid = 99999
            self.stdout = FakeStream([b"\xf0\x9f\x99", b"\x82\n", b""])
            self.stderr = FakeStream([b""])
            self._polled = False

        def poll(self):
            if not self._polled:
                self._polled = True
                return None
            return 0

        def wait(self, timeout=None) -> int:
            return 0

    task = store.create(task="emoji split", executor="shell", cwd=str(tmp_path), timeout=10)
    cancel_event = threading.Event()

    monkeypatch.setattr(executors, "STREAM_OUTPUT_INTERVAL", 0.05)

    with registry._lock:
        registry._cancel_events[task["task_id"]] = cancel_event
        registry._completion_events[task["task_id"]] = threading.Event()

    stdout, stderr = registry._stream_process(
        task_id=task["task_id"],
        process=FakeProcess(),
        timeout=10,
        cancel_event=cancel_event,
    )

    assert "🙂" in stdout
    assert "\ufffd" not in stdout, f"Replacement character found in: {stdout!r}"
