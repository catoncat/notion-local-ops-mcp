import time
from pathlib import Path

import pytest
import notion_local_ops_mcp.executors as executors
from notion_local_ops_mcp.executors import ExecutorRegistry, Invocation
from notion_local_ops_mcp.tasks import TaskStore
from tests.helpers import python_json_cmd, python_print_cmd, python_sleep_cmd


def test_executor_registry_prefers_codex_when_present(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    task = registry.submit(task="say hi", executor="auto", cwd=tmp_path, timeout=5)
    loaded = store.get(task["task_id"])

    assert loaded["executor"] == "codex"


def test_task_store_persists_status_updates(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    created = store.create(task="check", executor="codex", cwd=str(tmp_path))
    store.update(created["task_id"], status="running")
    loaded = store.get(created["task_id"])

    assert loaded["status"] == "running"


def test_submitted_task_eventually_succeeds(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("done"),
        claude_command=python_print_cmd("claude"),
    )

    task = registry.submit(task="finish", executor="codex", cwd=tmp_path, timeout=5)

    for _ in range(50):
        loaded = store.get(task["task_id"])
        if loaded["status"] == "succeeded":
            break
        time.sleep(0.05)

    loaded = store.get(task["task_id"])
    assert loaded["status"] == "succeeded"
    assert "done" in store.read_stdout(task["task_id"])


def test_cancel_marks_long_running_task_cancelled(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_sleep_cmd(2),
        claude_command=python_print_cmd("claude"),
    )

    task = registry.submit(task="cancel", executor="codex", cwd=tmp_path, timeout=5)
    cancelled = registry.cancel(task["task_id"])
    result = registry.wait(task["task_id"], timeout=2, poll_interval=0.05)

    assert cancelled["cancelled"] is True
    assert result["status"] == "cancelled"
    assert result["completed"] is True


def test_wait_returns_completed_task_metadata(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("done"),
        claude_command=python_print_cmd("claude"),
    )

    task = registry.submit(task="finish", executor="codex", cwd=tmp_path, timeout=5)
    result = registry.wait(task["task_id"], timeout=2, poll_interval=0.05)

    assert result["status"] == "succeeded"
    assert "done" in result["stdout_tail"]
    assert result["completed"] is True


def test_submit_command_runs_shell_task_in_background(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    task = registry.submit_command(
        command=python_print_cmd("shell"),
        cwd=tmp_path,
        timeout=5,
    )
    result = registry.wait(task["task_id"], timeout=2, poll_interval=0.05)

    assert result["executor"] == "shell"
    assert result["status"] == "succeeded"
    assert "shell" in result["stdout_tail"]
    assert result["completed"] is True


def test_cancel_marks_background_command_cancelled(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    task = registry.submit_command(
        command=python_sleep_cmd(2),
        cwd=tmp_path,
        timeout=5,
    )
    cancelled = registry.cancel(task["task_id"])
    result = registry.wait(task["task_id"], timeout=2, poll_interval=0.05)

    assert cancelled["cancelled"] is True
    assert cancelled["status"] == "cancelled"
    assert result["status"] == "cancelled"
    assert result["completed"] is True


def test_submit_persists_structured_delegate_metadata(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    task = registry.submit(
        task="Implement the fallback flow",
        goal="Ship a working fallback task runner",
        executor="codex",
        cwd=tmp_path,
        timeout=5,
        context_files=["README.md"],
        acceptance_criteria=["Tool returns structured status"],
        verification_commands=["pytest -q"],
        commit_mode="allowed",
    )
    stored = store.get(task["task_id"])

    assert stored["goal"] == "Ship a working fallback task runner"
    assert stored["acceptance_criteria"] == ["Tool returns structured status"]
    assert stored["verification_commands"] == ["pytest -q"]
    assert stored["commit_mode"] == "allowed"


def test_build_prompt_includes_structured_delegate_sections(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    prompt = registry._build_prompt(
        task="Implement the fallback flow",
        goal="Ship a working fallback task runner",
        context_files=["README.md", "src/app.py"],
        acceptance_criteria=["Tool returns structured status", "Tests pass"],
        verification_commands=["pytest -q", "python -m compileall src tests"],
        commit_mode="required",
    )

    assert "Goal:" in prompt
    assert "Ship a working fallback task runner" in prompt
    assert "Acceptance criteria:" in prompt
    assert "- Tool returns structured status" in prompt
    assert "Verification commands:" in prompt
    assert "- pytest -q" in prompt
    assert "Commit mode: required" in prompt


@pytest.mark.parametrize(
    ("executor_name", "command", "shim_path", "expected_prefix"),
    [
        (
            "codex",
            "codex",
            r"C:\Users\test\AppData\Local\Programs\Codex\bin\codex.cmd",
            ["exec", "--dangerously-bypass-approvals-and-sandbox", "-C"],
        ),
        (
            "claude-code",
            "claude",
            r"C:\Users\test\AppData\Local\Programs\Claude\bin\claude.cmd",
            ["--print", "--dangerously-skip-permissions", "--permission-mode"],
        ),
    ],
)
def test_build_invocation_resolves_windows_delegate_shim(
    tmp_path: Path,
    monkeypatch,
    executor_name: str,
    command: str,
    shim_path: str,
    expected_prefix: list[str],
) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command="codex",
        claude_command="claude",
    )

    monkeypatch.setattr(executors, "IS_WINDOWS", True)
    monkeypatch.setattr(
        executors.shutil,
        "which",
        lambda binary: shim_path if binary == command else None,
    )

    invocation = registry._build_invocation(
        executor_name=executor_name,
        command=command,
        task="Fix Windows startup",
        goal=None,
        cwd=tmp_path,
        context_files=[],
        acceptance_criteria=[],
        verification_commands=[],
        commit_mode="allowed",
    )

    assert invocation.use_shell is False
    assert invocation.args[0] == shim_path
    assert invocation.args[1:4] == expected_prefix


def test_run_task_decodes_utf8_process_output(tmp_path: Path, monkeypatch) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command="codex",
        claude_command="claude",
    )
    task = store.create(task="Run codex", executor="codex", cwd=str(tmp_path), timeout=5)

    monkeypatch.setattr(
        registry,
        "_build_invocation",
        lambda **_: Invocation(args=["codex"], use_shell=False),
    )

    popen_kwargs: dict[str, object] = {}

    class FakeStream:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self._read = False

        def read(self, n: int = -1) -> bytes:
            if self._read:
                return b""
            self._read = True
            return self._data

    class FakeProcess:
        def __init__(self, *args, **kwargs) -> None:
            popen_kwargs.update(kwargs)
            self.returncode = 0
            self.stdout = FakeStream(b"done \xe2\x98\x83\xff")
            self.stderr = FakeStream(b"warn \xff")
            self._polled = False

        def poll(self):
            if not self._polled:
                self._polled = True
                return None
            return 0

        def kill(self) -> None:
            return None

    monkeypatch.setattr(executors.subprocess, "Popen", FakeProcess)

    registry._run_task(
        task_id=task["task_id"],
        executor_name="codex",
        command="codex",
        task="Run codex",
        goal=None,
        cwd=tmp_path,
        timeout=5,
        cancel_event=executors.threading.Event(),
        context_files=[],
        acceptance_criteria=[],
        verification_commands=[],
        commit_mode="allowed",
        output_schema=None,
        parse_structured_output=True,
    )

    assert popen_kwargs["text"] is False
    assert store.get(task["task_id"])["status"] == "succeeded"
    assert store.read_stdout(task["task_id"]) == "done \u2603\ufffd"
    assert store.read_stderr(task["task_id"]) == "warn \ufffd"


def test_delegate_task_extracts_structured_json_output(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_json_cmd({"ok": True}),
        claude_command=python_print_cmd("claude"),
    )

    task = registry.submit(
        task="emit json",
        executor="codex",
        cwd=tmp_path,
        timeout=5,
        output_schema={"type": "object"},
        parse_structured_output=True,
    )
    result = registry.wait(task["task_id"], timeout=2, poll_interval=0.05)

    assert result["status"] == "succeeded"
    assert result["structured_output"] == {"ok": True}
