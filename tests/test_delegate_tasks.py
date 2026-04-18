import subprocess
import time
from pathlib import Path

from notion_local_ops_mcp.executors import ExecutorRegistry, _command_name, _normalized_command_parts
from notion_local_ops_mcp.tasks import TaskStore
from tests.helpers import commit_file, init_git_repo, python_command


def _build_registry(
    tmp_path: Path,
    *,
    codex_command: str | None = None,
    claude_command: str | None = None,
) -> ExecutorRegistry:
    return ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command=codex_command or python_command("print('codex')"),
        claude_command=claude_command or python_command("print('claude')"),
    )


def test_executor_registry_prefers_codex_when_present(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path)

    task = registry.submit(task="say hi", executor="auto", cwd=tmp_path, timeout=5)
    loaded = registry.store.get(task["task_id"])

    assert loaded["executor"] == "codex"


def test_task_store_persists_status_updates(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    created = store.create(task="check", executor="codex", cwd=str(tmp_path))
    store.update(created["task_id"], status="running")
    loaded = store.get(created["task_id"])

    assert loaded["status"] == "running"


def test_submitted_task_eventually_succeeds(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path, codex_command=python_command("print('done')"))

    task = registry.submit(task="finish", executor="codex", cwd=tmp_path, timeout=5)

    for _ in range(50):
        loaded = registry.store.get(task["task_id"])
        if loaded["status"] == "succeeded":
            break
        time.sleep(0.05)

    loaded = registry.store.get(task["task_id"])
    assert loaded["status"] == "succeeded"
    assert "done" in registry.store.read_stdout(task["task_id"])


def test_submitted_task_streams_logs_before_completion(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        codex_command=python_command("import time; print('started', flush=True); time.sleep(1.5); print('done', flush=True)"),
    )

    task = registry.submit(task="stream logs", executor="codex", cwd=tmp_path, timeout=5)

    saw_running_output = False
    for _ in range(30):
        loaded = registry.store.get(task["task_id"])
        stdout = registry.store.read_stdout(task["task_id"])
        if loaded["status"] == "running" and "started" in stdout:
            saw_running_output = True
            break
        time.sleep(0.1)

    result = registry.wait(task["task_id"], timeout=3, poll_interval=0.05)

    assert saw_running_output is True
    assert result["status"] == "succeeded"
    assert "done" in result["stdout_tail"]


def test_submitted_task_refreshes_updated_at_while_running(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path, codex_command=python_command("import time; time.sleep(2.5)"))

    task = registry.submit(task="heartbeat", executor="codex", cwd=tmp_path, timeout=5)

    first_running: dict[str, object] | None = None
    for _ in range(20):
        loaded = registry.store.get(task["task_id"])
        if loaded["status"] == "running":
            first_running = loaded
            break
        time.sleep(0.1)

    assert first_running is not None

    time.sleep(1.2)
    refreshed = registry.store.get(task["task_id"])
    result = registry.wait(task["task_id"], timeout=3, poll_interval=0.05)

    assert refreshed["status"] == "running"
    assert refreshed["updated_at"] != first_running["updated_at"]
    assert result["status"] == "succeeded"


def test_cancel_marks_long_running_task_cancelled(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path, codex_command=python_command("import time; time.sleep(2)"))

    task = registry.submit(task="cancel", executor="codex", cwd=tmp_path, timeout=5)
    cancelled = registry.cancel(task["task_id"])
    result = registry.wait(task["task_id"], timeout=2, poll_interval=0.05)

    assert cancelled["cancelled"] is True
    assert result["status"] == "cancelled"
    assert result["completed"] is True


def test_wait_returns_completed_task_metadata(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path, codex_command=python_command("print('done')"))

    task = registry.submit(task="finish", executor="codex", cwd=tmp_path, timeout=5)
    result = registry.wait(task["task_id"], timeout=2, poll_interval=0.05)

    assert result["status"] == "succeeded"
    assert "done" in result["stdout_tail"]
    assert result["completed"] is True


def test_submit_command_runs_shell_task_in_background(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path)

    task = registry.submit_command(
        command=python_command("print('shell')"),
        cwd=tmp_path,
        timeout=5,
    )
    result = registry.wait(task["task_id"], timeout=2, poll_interval=0.05)

    assert result["executor"] == "shell"
    assert result["status"] == "succeeded"
    assert "shell" in result["stdout_tail"]
    assert result["completed"] is True


def test_cancel_marks_background_command_cancelled(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path)

    task = registry.submit_command(
        command=python_command("import time; time.sleep(2)"),
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
    registry = _build_registry(tmp_path)

    task = registry.submit(
        task="Implement the fallback flow",
        goal="Ship a working fallback task runner",
        instructions="Keep logs structured.",
        executor="codex",
        cwd=tmp_path,
        timeout=5,
        context_files=["README.md"],
        acceptance_criteria=["Tool returns structured status"],
        verification_commands=["pytest -q"],
        commit_mode="allowed",
    )
    stored = registry.store.get(task["task_id"])

    assert stored["goal"] == "Ship a working fallback task runner"
    assert stored["instructions"] == "Keep logs structured."
    assert stored["acceptance_criteria"] == ["Tool returns structured status"]
    assert stored["verification_commands"] == ["pytest -q"]
    assert stored["commit_mode"] == "allowed"
    assert stored["mode"] == "exec"
    assert stored["selected_executor"] == "codex"
    assert stored["error"] is None


def test_build_prompt_includes_structured_delegate_sections(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path)

    prompt = registry._build_exec_prompt(
        task="Implement the fallback flow",
        goal="Ship a working fallback task runner",
        instructions="Prefer reversible changes.",
        context_files=["README.md", "src/app.py"],
        acceptance_criteria=["Tool returns structured status", "Tests pass"],
        verification_commands=["pytest -q", "python -m compileall src tests"],
        commit_mode="required",
    )

    assert "Goal:" in prompt
    assert "Ship a working fallback task runner" in prompt
    assert "Instructions:" in prompt
    assert "Prefer reversible changes." in prompt
    assert "Acceptance criteria:" in prompt
    assert "- Tool returns structured status" in prompt
    assert "Verification commands:" in prompt
    assert "- pytest -q" in prompt
    assert "Commit mode: required" in prompt


def test_command_name_uses_stem_for_windows_shims() -> None:
    assert _command_name(r"C:\Users\mingm\AppData\Roaming\npm\codex.cmd") == "codex"
    assert _command_name(r"C:\Users\mingm\AppData\Roaming\npm\claude.CMD") == "claude"


def test_normalized_command_parts_resolve_windows_cmd_shims(tmp_path: Path, monkeypatch) -> None:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    command_name = "demo-tool"
    cmd_path = shim_dir / f"{command_name}.cmd"
    bare_path = shim_dir / command_name
    cmd_path.write_text("@echo off\r\necho demo\r\n", encoding="utf-8")
    bare_path.write_text("placeholder", encoding="utf-8")

    monkeypatch.setenv("PATH", str(shim_dir))
    parts = _normalized_command_parts(command_name)

    assert Path(parts[0]).name.lower() == f"{command_name}.cmd"


def test_build_exec_invocation_resolves_windows_codex_shim(tmp_path: Path, monkeypatch) -> None:
    registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command="codex",
        claude_command="claude",
    )
    shim_path = r"C:\Users\test\AppData\Local\Programs\Codex\bin\codex.cmd"

    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("notion_local_ops_mcp.executors.shutil.which", lambda binary: shim_path if binary == "codex" else None)
    monkeypatch.setattr(registry, "_is_git_repo", lambda cwd: False)

    invocation = registry._build_exec_invocation(
        executor_name="codex",
        command="codex",
        cwd=tmp_path,
        task="Fix Windows startup",
        goal=None,
        instructions=None,
        context_files=[],
        acceptance_criteria=[],
        verification_commands=[],
        commit_mode="allowed",
        model=None,
    )

    assert invocation.use_shell is False
    assert isinstance(invocation.args, list)
    assert invocation.args[0] == shim_path
    assert invocation.args[1:4] == ["exec", "--dangerously-bypass-approvals-and-sandbox", "-C"]


def test_build_exec_invocation_resolves_windows_claude_shim(tmp_path: Path, monkeypatch) -> None:
    registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command="codex",
        claude_command="claude",
    )
    shim_path = r"C:\Users\test\AppData\Local\Programs\Claude\bin\claude.cmd"

    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("notion_local_ops_mcp.executors.shutil.which", lambda binary: shim_path if binary == "claude" else None)

    invocation = registry._build_exec_invocation(
        executor_name="claude-code",
        command="claude",
        cwd=tmp_path,
        task="Fix Windows startup",
        goal=None,
        instructions=None,
        context_files=[],
        acceptance_criteria=[],
        verification_commands=[],
        commit_mode="allowed",
        model=None,
    )

    assert invocation.use_shell is False
    assert isinstance(invocation.args, list)
    assert invocation.args[0] == shim_path
    assert invocation.args[1:4] == ["--print", "--dangerously-skip-permissions", "--permission-mode"]


def test_submitted_task_replaces_invalid_utf8_output(tmp_path: Path) -> None:
    registry = _build_registry(
        tmp_path,
        codex_command=python_command(
            "import sys; sys.stdout.buffer.write(b'done \\xff'); sys.stderr.buffer.write(b'warn \\xff')"
        ),
    )

    task = registry.submit(task="utf8", executor="codex", cwd=tmp_path, timeout=5)
    result = registry.wait(task["task_id"], timeout=2, poll_interval=0.05)

    assert result["status"] == "succeeded"
    assert "done �" in result["stdout_tail"]
    assert "warn �" in result["stderr_tail"]


def test_submit_returns_structured_error_for_unsupported_executor(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path)

    result = registry.submit(task="noop", executor="bogus", cwd=tmp_path, timeout=5)

    assert result["success"] is False
    assert result["error"]["code"] == "unsupported_executor"


def test_submit_returns_structured_error_for_missing_task_goal_and_instructions(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path)

    result = registry.submit(task=None, goal=None, instructions=None, executor="codex", cwd=tmp_path, timeout=5)

    assert result["success"] is False
    assert result["error"]["code"] == "invalid_request"


def test_delegate_task_alias_claudecode_maps_to_claude_code(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path)

    prepared = registry._prepare_exec_task(
        task="Run with alias",
        goal=None,
        instructions=None,
        executor="claudecode",
        cwd=tmp_path,
        context_files=None,
        acceptance_criteria=None,
        verification_commands=None,
        commit_mode="allowed",
        model=None,
    )

    assert not isinstance(prepared, dict)
    assert prepared.selected_executor == "claude-code"


def test_prepare_review_rejects_non_git_repo(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path)

    result = registry._prepare_review_task(
        executor="codex",
        cwd=tmp_path,
        task=None,
        goal=None,
        instructions=None,
        context_files=None,
        acceptance_criteria=None,
        verification_commands=None,
        commit_mode="forbidden",
        model=None,
        commit="HEAD",
        base_ref=None,
        head_ref=None,
        uncommitted=False,
        split_strategy="by_commit",
    )

    assert result["success"] is False
    assert result["error"]["code"] == "not_a_git_repo"


def test_prepare_review_rejects_invalid_commit(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    commit_file(tmp_path, "note.txt", "one\n", "first")
    registry = _build_registry(tmp_path)

    result = registry._prepare_review_task(
        executor="codex",
        cwd=tmp_path,
        task=None,
        goal=None,
        instructions=None,
        context_files=None,
        acceptance_criteria=None,
        verification_commands=None,
        commit_mode="forbidden",
        model=None,
        commit="deadbeef",
        base_ref=None,
        head_ref=None,
        uncommitted=False,
        split_strategy="by_commit",
    )

    assert result["success"] is False
    assert result["error"]["code"] == "invalid_review_range"


def test_get_wait_cancel_missing_task_are_structured_errors(tmp_path: Path) -> None:
    registry = _build_registry(tmp_path)

    assert registry.get("missing")["error"]["code"] == "task_not_found"
    assert registry.wait("missing", timeout=0.1)["error"]["code"] == "task_not_found"
    assert registry.cancel("missing")["error"]["code"] == "task_not_found"


def test_prepare_codex_review_commit_uses_native_review_command(tmp_path: Path, monkeypatch) -> None:
    init_git_repo(tmp_path)
    commit_sha = commit_file(tmp_path, "note.txt", "one\n", "first")
    monkeypatch.setattr("notion_local_ops_mcp.executors._command_available", lambda command: True)
    registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command="codex",
        claude_command="claude",
    )

    prepared = registry._prepare_review_task(
        executor="codex",
        cwd=tmp_path,
        task="Review this commit",
        goal=None,
        instructions="Focus on regressions.",
        context_files=None,
        acceptance_criteria=None,
        verification_commands=None,
        commit_mode="forbidden",
        model="gpt-5.4",
        commit=commit_sha,
        base_ref=None,
        head_ref=None,
        uncommitted=False,
        split_strategy="by_commit",
    )

    assert not isinstance(prepared, dict)
    args = prepared.invocations[0].args
    assert isinstance(args, list)
    assert "review" in args
    assert "--commit" in args
    assert args[args.index("--commit") + 1] == commit_sha
    assert args[args.index("--model") + 1] == "gpt-5.4"


def test_prepare_codex_review_uncommitted_uses_native_review_command(tmp_path: Path, monkeypatch) -> None:
    init_git_repo(tmp_path)
    commit_file(tmp_path, "note.txt", "one\n", "first")
    (tmp_path / "note.txt").write_text("two\n", encoding="utf-8")
    monkeypatch.setattr("notion_local_ops_mcp.executors._command_available", lambda command: True)
    registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command="codex",
        claude_command="claude",
    )

    prepared = registry._prepare_review_task(
        executor="codex",
        cwd=tmp_path,
        task=None,
        goal=None,
        instructions=None,
        context_files=None,
        acceptance_criteria=None,
        verification_commands=None,
        commit_mode="forbidden",
        model=None,
        commit=None,
        base_ref=None,
        head_ref=None,
        uncommitted=True,
        split_strategy="by_commit",
    )

    assert not isinstance(prepared, dict)
    args = prepared.invocations[0].args
    assert isinstance(args, list)
    assert "review" in args
    assert "--uncommitted" in args


def test_prepare_claude_review_uses_print_mode_and_prompt(tmp_path: Path, monkeypatch) -> None:
    init_git_repo(tmp_path)
    commit_sha = commit_file(tmp_path, "note.txt", "one\n", "first")
    monkeypatch.setattr("notion_local_ops_mcp.executors._command_available", lambda command: True)
    registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command="codex",
        claude_command="claude",
    )

    prepared = registry._prepare_review_task(
        executor="claude",
        cwd=tmp_path,
        task="Review with Claude",
        goal=None,
        instructions="Focus on missing tests.",
        context_files=None,
        acceptance_criteria=None,
        verification_commands=None,
        commit_mode="forbidden",
        model="sonnet",
        commit=commit_sha,
        base_ref=None,
        head_ref=None,
        uncommitted=False,
        split_strategy="by_commit",
    )

    assert not isinstance(prepared, dict)
    args = prepared.invocations[0].args
    assert isinstance(args, list)
    assert "--print" in args
    assert "--model" in args
    assert args[-1].startswith("Perform a code review only.")


def test_review_range_defaults_to_by_commit_splitting(tmp_path: Path, monkeypatch) -> None:
    init_git_repo(tmp_path)
    base = commit_file(tmp_path, "note.txt", "one\n", "first")
    commit_file(tmp_path, "note.txt", "two\n", "second")
    commit_file(tmp_path, "note.txt", "three\n", "third")
    monkeypatch.setattr("notion_local_ops_mcp.executors._command_available", lambda command: True)
    registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command="codex",
        claude_command="claude",
    )

    prepared = registry._prepare_review_task(
        executor="codex",
        cwd=tmp_path,
        task=None,
        goal=None,
        instructions=None,
        context_files=None,
        acceptance_criteria=None,
        verification_commands=None,
        commit_mode="forbidden",
        model=None,
        commit=None,
        base_ref=base,
        head_ref="HEAD",
        uncommitted=False,
        split_strategy="by_commit",
    )

    assert not isinstance(prepared, dict)
    assert len(prepared.invocations) == 2


def test_review_range_single_strategy_keeps_one_invocation(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    base = commit_file(tmp_path, "note.txt", "one\n", "first")
    commit_file(tmp_path, "note.txt", "two\n", "second")
    registry = _build_registry(tmp_path)

    prepared = registry._prepare_review_task(
        executor="claude",
        cwd=tmp_path,
        task=None,
        goal=None,
        instructions=None,
        context_files=None,
        acceptance_criteria=None,
        verification_commands=None,
        commit_mode="forbidden",
        model=None,
        commit=None,
        base_ref=base,
        head_ref="HEAD",
        uncommitted=False,
        split_strategy="single",
    )

    assert not isinstance(prepared, dict)
    assert len(prepared.invocations) == 1


def test_registry_recovers_incomplete_tasks_as_interrupted(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    queued = store.create(task="queued", executor="codex", cwd=str(tmp_path))
    running = store.create(task="running", executor="codex", cwd=str(tmp_path))
    store.update(running["task_id"], status="running")

    registry = ExecutorRegistry(
        store=store,
        codex_command=python_command("print('codex')"),
        claude_command=python_command("print('claude')"),
    )

    _ = registry
    queued_meta = store.get(queued["task_id"])
    running_meta = store.get(running["task_id"])

    assert queued_meta["status"] == "interrupted"
    assert queued_meta["error"]["code"] == "task_interrupted"
    assert running_meta["status"] == "interrupted"
    assert "server restarted before task completion" in store.read_summary(running["task_id"])


def test_doctor_reports_mcp_auth_warning(tmp_path: Path, monkeypatch) -> None:
    registry = _build_registry(tmp_path)

    def fake_probe(command: str, args: list[str], *, cwd: Path | None, timeout: int = 10) -> subprocess.CompletedProcess[str]:
        if args == ["--help"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="help\n", stderr="")
        if args == ["--version"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="codex 1.2.3\n", stderr="")
        if args == ["exec", "review", "--help"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="review help\n", stderr="")
        if args == ["mcp", "list"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="Name Url Status Auth\ncloudflare-api https://mcp.cloudflare.com/mcp enabled Not logged in\n",
                stderr="",
            )
        raise AssertionError(f"Unexpected probe: {args}")

    monkeypatch.setattr(registry, "_probe_command", fake_probe)
    monkeypatch.setattr(registry, "_is_git_repo", lambda cwd: True)

    result = registry.doctor(cwd=tmp_path, executor="codex")

    assert result["success"] is True
    assert result["warnings"][0]["code"] == "mcp_auth_warning"
    assert result["selected_executor"] == "codex"
