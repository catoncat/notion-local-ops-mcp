from __future__ import annotations

import threading
import time
from pathlib import Path

from notion_local_ops_mcp.executors import ExecutorRegistry
from notion_local_ops_mcp.tasks import TaskStore
from tests.helpers import python_print_cmd, python_sleep_cmd


def _registry(tmp_path: Path) -> ExecutorRegistry:
    store = TaskStore(tmp_path / "state")
    return ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("done"),
        claude_command=python_print_cmd("claude"),
    )


def test_wait_returns_quickly_via_completion_event(tmp_path: Path) -> None:
    # run_command task that exits in ~0.05s; wait_task should return within
    # ~0.1s thanks to the event. If we were still polling at 0.5s this would
    # take at least half a second.
    registry = _registry(tmp_path)
    submitted = registry.submit_command(
        command=python_sleep_cmd(0.05),
        cwd=tmp_path,
        timeout=10,
    )
    task_id = submitted["task_id"]
    start = time.monotonic()
    # Poll interval deliberately set large: proves we are NOT polling.
    result = registry.wait(task_id, timeout=5, poll_interval=2.0)
    elapsed = time.monotonic() - start
    assert result["completed"] is True, result
    assert result["timed_out"] is False
    assert result["status"] == "succeeded"
    assert elapsed < 0.5, f"wait took {elapsed:.3f}s, event path not used"


def test_wait_times_out_when_task_still_running(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    submitted = registry.submit_command(
        command=python_sleep_cmd(2),
        cwd=tmp_path,
        timeout=10,
    )
    task_id = submitted["task_id"]
    start = time.monotonic()
    result = registry.wait(task_id, timeout=0.1, poll_interval=2.0)
    elapsed = time.monotonic() - start
    assert result["completed"] is False
    assert result["timed_out"] is True
    assert 0.05 <= elapsed < 1.0, f"unexpected wait elapsed {elapsed:.3f}s"
    # let the background task finish so pytest doesn't leak subprocesses
    registry.wait(task_id, timeout=10)


def test_wait_fast_path_when_task_already_completed(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    submitted = registry.submit_command(
        command=python_print_cmd("1"),
        cwd=tmp_path,
        timeout=10,
    )
    task_id = submitted["task_id"]
    # Block on the event once to make sure it's finished.
    registry.wait(task_id, timeout=5)
    start = time.monotonic()
    result = registry.wait(task_id, timeout=5)
    elapsed = time.monotonic() - start
    assert result["completed"] is True
    assert result["timed_out"] is False
    assert elapsed < 0.05, f"fast path too slow: {elapsed:.4f}s"


def test_wait_falls_back_to_polling_when_event_missing(tmp_path: Path) -> None:
    # Simulates a task persisted from a previous process: meta.json is on disk
    # but no completion event is registered on this registry instance.
    store = TaskStore(tmp_path / "state")
    created = store.create(task="legacy", executor="codex", cwd=str(tmp_path))
    registry = ExecutorRegistry(store=store, codex_command=None, claude_command=None)

    # Flip the task to succeeded from another thread after a short delay.
    def _finish() -> None:
        time.sleep(0.1)
        store.update(created["task_id"], status="succeeded")

    threading.Thread(target=_finish, daemon=True).start()

    start = time.monotonic()
    result = registry.wait(created["task_id"], timeout=2, poll_interval=0.05)
    elapsed = time.monotonic() - start
    assert result["completed"] is True
    assert result["timed_out"] is False
    assert 0.05 <= elapsed < 1.0, f"polling fallback elapsed {elapsed:.3f}s"


def test_cancel_wakes_waiters(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    submitted = registry.submit_command(
        command=python_sleep_cmd(10),
        cwd=tmp_path,
        timeout=30,
    )
    task_id = submitted["task_id"]

    def _cancel_after_delay() -> None:
        time.sleep(0.1)
        registry.cancel(task_id)

    threading.Thread(target=_cancel_after_delay, daemon=True).start()
    start = time.monotonic()
    result = registry.wait(task_id, timeout=5, poll_interval=2.0)
    elapsed = time.monotonic() - start
    assert result["completed"] is True
    assert result["status"] == "cancelled"
    assert elapsed < 1.0, f"cancel didn't wake waiter quickly: {elapsed:.3f}s"
