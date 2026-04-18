from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from .tasks import TaskNotFoundError, TaskStore


TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}
ALLOWED_COMMIT_MODES = {"allowed", "required", "forbidden"}
ALLOWED_REVIEW_SPLIT_STRATEGIES = {"by_commit", "single"}
EXECUTOR_ALIASES = {
    "auto": "auto",
    "codex": "codex",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "claudecode": "claude-code",
    "claude_code": "claude-code",
}
LOG_FLUSH_INTERVAL_SECONDS = 0.25
HEARTBEAT_INTERVAL_SECONDS = 1.0
PROCESS_POLL_INTERVAL_SECONDS = 0.1
STREAM_JOIN_TIMEOUT_SECONDS = 1.0
CHECK_TIMEOUT_SECONDS = 10


def _split_command(command: str) -> list[str]:
    parts = shlex.split(command, posix=os.name != "nt")
    normalized: list[str] = []
    for part in parts:
        if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}:
            normalized.append(part[1:-1])
        else:
            normalized.append(part)
    return normalized


def _resolve_binary(binary: str) -> str:
    path = Path(binary)
    if path.exists():
        return str(path)
    resolved = shutil.which(binary)
    if resolved:
        return resolved
    return binary


def _normalized_command_parts(command: str) -> list[str]:
    parts = _split_command(command)
    if not parts:
        return []
    parts[0] = _resolve_binary(parts[0])
    return parts


def _command_name(command_part: str) -> str:
    return Path(command_part).stem.lower()


def _command_available(command: str | None) -> bool:
    if not command:
        return False
    parts = _split_command(command)
    if not parts:
        return False
    binary = _resolve_binary(parts[0])
    if Path(binary).exists():
        return True
    return shutil.which(binary) is not None


def _shell_join(parts: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def _truncate_text(value: str, max_length: int = 240) -> str:
    if len(value) <= max_length:
        return value
    return f"{value[: max_length - 3]}..."


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _error_response(code: str, message: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": False,
        "error": _issue(code, message),
    }
    payload.update(extra)
    return payload


def _summarize(stdout: str, stderr: str) -> str:
    for candidate in (stdout.strip(), stderr.strip()):
        if candidate:
            return candidate.splitlines()[-1]
    return ""


@dataclass(frozen=True)
class Invocation:
    args: list[str] | str
    use_shell: bool
    label: str


@dataclass(frozen=True)
class PreparedTask:
    task: str
    mode: str
    selected_executor: str
    resolved_command: str
    metadata: dict[str, object]
    invocations: list[Invocation]


@dataclass(frozen=True)
class ProcessOutcome:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool = False
    error: dict[str, str] | None = None


@dataclass(frozen=True)
class ResolvedExecutor:
    requested_executor: str
    selected_executor: str | None
    command: str | None
    available_executors: list[str]
    error: dict[str, str] | None = None


@dataclass
class StreamCapture:
    chunks: list[str] = field(default_factory=list)
    flushed_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: threading.Event = field(default_factory=threading.Event)

    def append(self, chunk: str) -> None:
        with self.lock:
            self.chunks.append(chunk)

    def read_pending(self) -> str:
        with self.lock:
            if self.flushed_count >= len(self.chunks):
                return ""
            content = "".join(self.chunks[self.flushed_count :])
            self.flushed_count = len(self.chunks)
            return content

    def read_all(self) -> str:
        with self.lock:
            return "".join(self.chunks)


class ExecutorRegistry:
    def __init__(self, *, store: TaskStore, codex_command: str | None, claude_command: str | None) -> None:
        self.store = store
        self.codex_command = codex_command
        self.claude_command = claude_command
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self.store.recover_incomplete()

    def submit(
        self,
        *,
        task: str | None,
        goal: str | None = None,
        instructions: str | None = None,
        executor: str,
        cwd: Path,
        timeout: int,
        context_files: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        verification_commands: list[str] | None = None,
        commit_mode: str = "allowed",
        model: str | None = None,
    ) -> dict[str, object]:
        prepared = self._prepare_exec_task(
            task=task,
            goal=goal,
            instructions=instructions,
            executor=executor,
            cwd=cwd,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            verification_commands=verification_commands,
            commit_mode=commit_mode,
            model=model,
        )
        if isinstance(prepared, dict):
            return prepared
        return self._start_prepared_task(prepared=prepared, cwd=cwd, timeout=timeout)

    def submit_review(
        self,
        *,
        executor: str,
        cwd: Path,
        timeout: int,
        task: str | None = None,
        goal: str | None = None,
        instructions: str | None = None,
        context_files: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        verification_commands: list[str] | None = None,
        commit_mode: str = "allowed",
        model: str | None = None,
        commit: str | None = None,
        base_ref: str | None = None,
        head_ref: str | None = None,
        uncommitted: bool = False,
        split_strategy: str = "by_commit",
    ) -> dict[str, object]:
        prepared = self._prepare_review_task(
            executor=executor,
            cwd=cwd,
            task=task,
            goal=goal,
            instructions=instructions,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            verification_commands=verification_commands,
            commit_mode=commit_mode,
            model=model,
            commit=commit,
            base_ref=base_ref,
            head_ref=head_ref,
            uncommitted=uncommitted,
            split_strategy=split_strategy,
        )
        if isinstance(prepared, dict):
            return prepared
        return self._start_prepared_task(prepared=prepared, cwd=cwd, timeout=timeout)

    def submit_command(
        self,
        *,
        command: str,
        cwd: Path,
        timeout: int,
    ) -> dict[str, object]:
        cwd_error = self._validate_cwd(cwd)
        if cwd_error:
            return cwd_error
        prepared = PreparedTask(
            task=command,
            mode="command",
            selected_executor="shell",
            resolved_command=command,
            metadata={
                "mode": "command",
                "selected_executor": "shell",
                "resolved_command": command,
                "invocation_preview": _truncate_text(command),
                "preflight": {"cwd_ok": True},
            },
            invocations=[Invocation(args=command, use_shell=True, label="shell command")],
        )
        return self._start_prepared_task(prepared=prepared, cwd=cwd, timeout=timeout)

    def doctor(
        self,
        *,
        cwd: Path,
        executor: str = "auto",
        check_git: bool = True,
        check_mcp_auth: bool = True,
    ) -> dict[str, object]:
        resolved = self._resolve_executor(executor)
        warnings: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        cwd_ok = False
        cwd_error = self._validate_cwd(cwd)
        if cwd_error:
            errors.append(cwd_error["error"])  # type: ignore[arg-type]
        else:
            cwd_ok = True

        git_repo = self._is_git_repo(cwd) if check_git and cwd_ok else False
        if check_git and cwd_ok and not git_repo:
            warnings.append(_issue("not_a_git_repo", f"Not a git repository: {cwd}"))

        version = ""
        if resolved.error:
            errors.append(resolved.error)
        elif resolved.selected_executor and resolved.command:
            selected_command = resolved.command
            help_probe = self._probe_command(selected_command, ["--help"], cwd=cwd if cwd_ok else None)
            if help_probe.returncode != 0:
                errors.append(
                    _issue(
                        "executor_not_available",
                        f"{resolved.selected_executor} help probe failed with exit code {help_probe.returncode}.",
                    )
                )
            version_probe = self._probe_command(selected_command, ["--version"], cwd=cwd if cwd_ok else None)
            if version_probe.returncode == 0:
                raw_version = (version_probe.stdout or version_probe.stderr).strip()
                version = raw_version.splitlines()[0] if raw_version else ""
            if resolved.selected_executor == "codex":
                review_probe = self._probe_command(selected_command, ["exec", "review", "--help"], cwd=cwd if cwd_ok else None)
                if review_probe.returncode != 0:
                    errors.append(_issue("executor_not_available", "codex exec review --help failed."))
                if check_mcp_auth:
                    mcp_probe = self._probe_command(selected_command, ["mcp", "list"], cwd=cwd if cwd_ok else None)
                    for line in (mcp_probe.stdout or "").splitlines():
                        lowered = line.lower()
                        if "http" in lowered and "not logged in" in lowered:
                            warnings.append(_issue("mcp_auth_warning", line.strip()))
            elif resolved.selected_executor == "claude-code" and check_mcp_auth:
                mcp_probe = self._probe_command(selected_command, ["mcp", "list"], cwd=cwd if cwd_ok else None)
                for line in (mcp_probe.stdout or "").splitlines():
                    lowered = line.lower()
                    if any(token in lowered for token in ["✗", "unhealthy", "failed", "not connected", "error"]):
                        warnings.append(_issue("mcp_health_warning", line.strip()))

        return {
            "success": len(errors) == 0,
            "requested_executor": executor,
            "selected_executor": resolved.selected_executor,
            "available_executors": resolved.available_executors,
            "resolved_command": self._resolved_command_string(resolved.command) if resolved.command else None,
            "version": version,
            "cwd_ok": cwd_ok,
            "git_repo": git_repo,
            "warnings": warnings,
            "errors": errors,
        }

    def get(self, task_id: str) -> dict[str, object]:
        try:
            meta = self.store.get(task_id)
        except TaskNotFoundError:
            return _error_response("task_not_found", f"Task not found: {task_id}", task_id=task_id)
        meta["summary"] = self.store.read_summary(task_id)
        meta["stdout_tail"] = self.store.read_stdout(task_id)[-4000:]
        meta["stderr_tail"] = self.store.read_stderr(task_id)[-4000:]
        meta["artifacts"] = []
        meta["completed"] = meta["status"] in TERMINAL_TASK_STATUSES
        meta["success"] = True
        return meta

    def wait(self, task_id: str, timeout: float, poll_interval: float = 0.5) -> dict[str, object]:
        deadline = time.monotonic() + max(timeout, 0)
        interval = max(poll_interval, 0.05)
        while True:
            meta = self.get(task_id)
            if meta.get("success") is False:
                return meta
            if meta["completed"]:
                meta["timed_out"] = False
                return meta
            if time.monotonic() >= deadline:
                meta["timed_out"] = True
                return meta
            time.sleep(interval)

    def cancel(self, task_id: str) -> dict[str, object]:
        meta = self.store.get_optional(task_id)
        if meta is None:
            return _error_response("task_not_found", f"Task not found: {task_id}", task_id=task_id)
        if meta["status"] in TERMINAL_TASK_STATUSES:
            return {
                "success": True,
                "task_id": task_id,
                "status": meta["status"],
                "cancelled": False,
            }
        with self._lock:
            cancel_event = self._cancel_events.get(task_id)
            process = self._processes.get(task_id)
        if cancel_event is not None:
            cancel_event.set()
        if process is not None and process.poll() is None:
            process.kill()
        updated = self.store.update(task_id, status="cancelled", error=None)
        return {
            "success": True,
            "task_id": task_id,
            "status": updated["status"],
            "cancelled": True,
        }

    def list_tasks(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        executor: str | None = None,
    ) -> dict[str, object]:
        listing = self.store.list_tasks(limit=limit, offset=offset, status=status, executor=executor)
        items: list[dict[str, object]] = []
        for item in listing["items"]:
            entry = dict(item)
            entry["completed"] = entry.get("status") in TERMINAL_TASK_STATUSES
            items.append(entry)
        return {
            "success": True,
            "items": items,
            "total": listing["total"],
            "offset": listing["offset"],
            "limit": listing["limit"],
            "has_more": listing["has_more"],
        }

    def _prepare_exec_task(
        self,
        *,
        task: str | None,
        goal: str | None,
        instructions: str | None,
        executor: str,
        cwd: Path,
        context_files: list[str] | None,
        acceptance_criteria: list[str] | None,
        verification_commands: list[str] | None,
        commit_mode: str,
        model: str | None,
    ) -> PreparedTask | dict[str, object]:
        normalized_task = (task or "").strip()
        normalized_goal = (goal or "").strip()
        normalized_instructions = (instructions or "").strip()
        if not any([normalized_task, normalized_goal, normalized_instructions]):
            return _error_response("invalid_request", "delegate_task requires task, goal, or instructions.")
        if commit_mode not in ALLOWED_COMMIT_MODES:
            return _error_response("invalid_request", f"Unsupported commit_mode: {commit_mode}")
        cwd_error = self._validate_cwd(cwd)
        if cwd_error:
            return cwd_error
        resolved = self._resolve_executor(executor)
        if resolved.error or not resolved.selected_executor or not resolved.command:
            return _error_response(
                resolved.error["code"] if resolved.error else "executor_not_available",
                resolved.error["message"] if resolved.error else "No delegate executor command is available.",
                available_executors=resolved.available_executors,
                requested_executor=executor,
            )
        invocation = self._build_exec_invocation(
            executor_name=resolved.selected_executor,
            command=resolved.command,
            cwd=cwd,
            task=normalized_task or None,
            goal=normalized_goal or None,
            instructions=normalized_instructions or None,
            context_files=context_files or [],
            acceptance_criteria=acceptance_criteria or [],
            verification_commands=verification_commands or [],
            commit_mode=commit_mode,
            model=model,
        )
        metadata = {
            "mode": "exec",
            "goal": normalized_goal or None,
            "instructions": normalized_instructions or None,
            "acceptance_criteria": acceptance_criteria or [],
            "verification_commands": verification_commands or [],
            "commit_mode": commit_mode,
            "model": model,
            "requested_executor": executor,
            "selected_executor": resolved.selected_executor,
            "resolved_command": self._resolved_command_string(resolved.command),
            "invocation_preview": self._preview_invocation(invocation),
            "context_files": context_files or [],
            "preflight": {
                "cwd_ok": True,
                "git_repo": self._is_git_repo(cwd),
                "available_executors": resolved.available_executors,
            },
            "error": None,
        }
        return PreparedTask(
            task=normalized_task or normalized_goal or normalized_instructions,
            mode="exec",
            selected_executor=resolved.selected_executor,
            resolved_command=self._resolved_command_string(resolved.command),
            metadata=metadata,
            invocations=[invocation],
        )

    def _prepare_review_task(
        self,
        *,
        executor: str,
        cwd: Path,
        task: str | None,
        goal: str | None,
        instructions: str | None,
        context_files: list[str] | None,
        acceptance_criteria: list[str] | None,
        verification_commands: list[str] | None,
        commit_mode: str,
        model: str | None,
        commit: str | None,
        base_ref: str | None,
        head_ref: str | None,
        uncommitted: bool,
        split_strategy: str,
    ) -> PreparedTask | dict[str, object]:
        normalized_task = (task or "").strip()
        normalized_goal = (goal or "").strip()
        normalized_instructions = (instructions or "").strip()
        normalized_commit = (commit or "").strip() or None
        normalized_base_ref = (base_ref or "").strip() or None
        normalized_head_ref = (head_ref or "").strip() or None

        if commit_mode not in ALLOWED_COMMIT_MODES:
            return _error_response("invalid_request", f"Unsupported commit_mode: {commit_mode}")
        if split_strategy not in ALLOWED_REVIEW_SPLIT_STRATEGIES:
            return _error_response("invalid_request", f"Unsupported split_strategy: {split_strategy}")
        cwd_error = self._validate_cwd(cwd)
        if cwd_error:
            return cwd_error
        resolved = self._resolve_executor(executor)
        if resolved.error or not resolved.selected_executor or not resolved.command:
            return _error_response(
                resolved.error["code"] if resolved.error else "executor_not_available",
                resolved.error["message"] if resolved.error else "No delegate executor command is available.",
                available_executors=resolved.available_executors,
                requested_executor=executor,
            )
        if not self._is_git_repo(cwd):
            return _error_response("not_a_git_repo", f"Not a git repository: {cwd}")

        target_kinds = [bool(normalized_commit), bool(normalized_base_ref), bool(uncommitted)]
        if sum(target_kinds) != 1:
            return _error_response(
                "invalid_request",
                "Review requires exactly one target: commit, base_ref (optionally with head_ref), or uncommitted=true.",
            )
        if normalized_head_ref and not normalized_base_ref:
            return _error_response("invalid_request", "head_ref requires base_ref.")

        review_instructions = self._build_review_instructions(
            task=normalized_task or None,
            goal=normalized_goal or None,
            instructions=normalized_instructions or None,
            context_files=context_files or [],
            acceptance_criteria=acceptance_criteria or [],
            verification_commands=verification_commands or [],
            commit_mode=commit_mode,
        )

        if normalized_commit:
            commit_sha = self._resolve_commit_ref(cwd, normalized_commit)
            if not commit_sha:
                return _error_response("invalid_review_range", f"Commit not found: {normalized_commit}")
            target_description = f"commit {commit_sha}"
            invocations = self._build_review_invocations(
                executor_name=resolved.selected_executor,
                command=resolved.command,
                cwd=cwd,
                model=model,
                split_strategy=split_strategy,
                commit=commit_sha,
                base_ref=None,
                head_ref=None,
                instructions=review_instructions,
            )
            plan_info: dict[str, Any] = {
                "target": {"kind": "commit", "commit": commit_sha},
                "target_description": target_description,
            }
        elif uncommitted:
            target_description = "uncommitted changes"
            invocations = self._build_review_invocations(
                executor_name=resolved.selected_executor,
                command=resolved.command,
                cwd=cwd,
                model=model,
                split_strategy=split_strategy,
                commit=None,
                base_ref=None,
                head_ref=None,
                uncommitted=True,
                instructions=review_instructions,
            )
            plan_info = {
                "target": {"kind": "uncommitted"},
                "target_description": target_description,
            }
        else:
            assert normalized_base_ref is not None
            resolved_base_ref = self._resolve_commit_ref(cwd, normalized_base_ref)
            if not resolved_base_ref:
                return _error_response("invalid_review_range", f"Base ref not found: {normalized_base_ref}")
            normalized_head_ref = normalized_head_ref or "HEAD"
            resolved_head_ref = self._resolve_commit_ref(cwd, normalized_head_ref)
            if not resolved_head_ref:
                return _error_response("invalid_review_range", f"Head ref not found: {normalized_head_ref}")
            commits = self._list_commits_in_range(cwd, normalized_base_ref, normalized_head_ref)
            if split_strategy == "by_commit" and not commits:
                return _error_response(
                    "invalid_review_range",
                    f"No commits found in range {normalized_base_ref}..{normalized_head_ref}",
                )
            target_description = f"range {normalized_base_ref}..{normalized_head_ref}"
            invocations = self._build_review_invocations(
                executor_name=resolved.selected_executor,
                command=resolved.command,
                cwd=cwd,
                model=model,
                split_strategy=split_strategy,
                commit=None,
                base_ref=normalized_base_ref,
                head_ref=normalized_head_ref,
                commits=commits,
                instructions=review_instructions,
            )
            plan_info = {
                "target": {
                    "kind": "range",
                    "base_ref": normalized_base_ref,
                    "head_ref": normalized_head_ref,
                    "commits": commits,
                },
                "target_description": target_description,
            }

        metadata = {
            "mode": "review",
            "goal": normalized_goal or None,
            "instructions": normalized_instructions or None,
            "acceptance_criteria": acceptance_criteria or [],
            "verification_commands": verification_commands or [],
            "commit_mode": commit_mode,
            "model": model,
            "requested_executor": executor,
            "selected_executor": resolved.selected_executor,
            "resolved_command": self._resolved_command_string(resolved.command),
            "split_strategy": split_strategy,
            "invocation_preview": self._preview_invocation(invocations[0]),
            "invocation_previews": [self._preview_invocation(invocation) for invocation in invocations],
            "context_files": context_files or [],
            "preflight": {
                "cwd_ok": True,
                "git_repo": True,
                "available_executors": resolved.available_executors,
                **plan_info,
            },
            "error": None,
        }
        return PreparedTask(
            task=normalized_task or normalized_goal or f"Review {target_description}",
            mode="review",
            selected_executor=resolved.selected_executor,
            resolved_command=self._resolved_command_string(resolved.command),
            metadata=metadata,
            invocations=invocations,
        )

    def _start_prepared_task(self, *, prepared: PreparedTask, cwd: Path, timeout: int) -> dict[str, object]:
        created = self.store.create(
            task=prepared.task,
            executor=prepared.selected_executor,
            cwd=str(cwd),
            timeout=timeout,
            context_files=prepared.metadata.get("context_files", []),  # type: ignore[arg-type]
            metadata=prepared.metadata,
        )
        cancel_event = threading.Event()
        with self._lock:
            self._cancel_events[created["task_id"]] = cancel_event
        thread = threading.Thread(
            target=self._run_prepared_task,
            args=(created["task_id"], prepared.invocations, cwd, timeout, cancel_event),
            daemon=True,
        )
        thread.start()
        return {
            "success": True,
            "task_id": created["task_id"],
            "executor": prepared.selected_executor,
            "status": created["status"],
            "mode": prepared.mode,
        }

    def _run_prepared_task(
        self,
        task_id: str,
        invocations: list[Invocation],
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event,
    ) -> None:
        if cancel_event.is_set():
            self.store.update(task_id, status="cancelled", error=None)
            return

        self.store.update(task_id, status="running", total_steps=len(invocations), current_step=0, error=None)
        deadline = time.monotonic() + max(timeout, 0)
        last_exit_code = 0

        for index, invocation in enumerate(invocations, start=1):
            if cancel_event.is_set():
                self.store.update(task_id, status="cancelled", current_step=index - 1, error=None)
                return

            if len(invocations) > 1:
                self.store.append_logs(task_id, stdout=f"\n=== step {index}/{len(invocations)}: {invocation.label} ===\n")

            self.store.update(
                task_id,
                status="running",
                current_step=index,
                invocation_preview=self._preview_invocation(invocation),
            )
            remaining_timeout = max(int(deadline - time.monotonic()), 0)
            outcome = self._execute_invocation(
                task_id=task_id,
                invocation=invocation,
                cwd=cwd,
                timeout=remaining_timeout,
                cancel_event=cancel_event,
            )

            if cancel_event.is_set() or self.store.get(task_id)["status"] == "cancelled":
                self.store.write_summary(task_id, _summarize(outcome.stdout, outcome.stderr))
                self.store.update(task_id, status="cancelled", exit_code=outcome.exit_code, error=None)
                return

            if outcome.error:
                summary = _summarize(outcome.stdout, outcome.stderr) or outcome.error["message"]
                self.store.write_summary(task_id, summary)
                self.store.update(
                    task_id,
                    status="failed",
                    exit_code=outcome.exit_code,
                    timed_out=outcome.timed_out,
                    error=outcome.error,
                )
                return

            last_exit_code = outcome.exit_code or 0

        stdout = self.store.read_stdout(task_id)
        stderr = self.store.read_stderr(task_id)
        self.store.write_summary(task_id, _summarize(stdout, stderr))
        self.store.update(task_id, status="succeeded", exit_code=last_exit_code, timed_out=False, error=None)

    def _execute_invocation(
        self,
        *,
        task_id: str,
        invocation: Invocation,
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event,
    ) -> ProcessOutcome:
        try:
            process = subprocess.Popen(
                invocation.args,
                cwd=str(cwd),
                shell=invocation.use_shell,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self.store.append_logs(task_id, stderr=f"{exc}\n")
            return ProcessOutcome(
                stdout="",
                stderr=str(exc),
                exit_code=None,
                error=_issue("spawn_failed", f"Failed to start {invocation.label}: {exc}"),
            )

        stdout, stderr, timed_out = self._monitor_process(
            task_id=task_id,
            process=process,
            timeout=timeout,
            cancel_event=cancel_event,
        )
        if cancel_event.is_set():
            return ProcessOutcome(stdout=stdout, stderr=stderr, exit_code=process.returncode)
        if timed_out:
            return ProcessOutcome(
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode,
                timed_out=True,
                error=_issue("task_timed_out", f"Task timed out after {timeout} seconds while running {invocation.label}."),
            )
        if process.returncode not in (0, None):
            return ProcessOutcome(
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode,
                error=_issue("task_failed", f"{invocation.label} exited with code {process.returncode}."),
            )
        return ProcessOutcome(stdout=stdout, stderr=stderr, exit_code=process.returncode or 0)

    def _monitor_process(
        self,
        *,
        task_id: str,
        process: subprocess.Popen[str],
        timeout: int,
        cancel_event: threading.Event,
    ) -> tuple[str, str, bool]:
        stdout_capture = StreamCapture()
        stderr_capture = StreamCapture()
        readers = [
            threading.Thread(target=self._capture_stream, args=(process.stdout, stdout_capture), daemon=True),
            threading.Thread(target=self._capture_stream, args=(process.stderr, stderr_capture), daemon=True),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        deadline = time.monotonic() + max(timeout, 0)
        next_flush = time.monotonic()
        next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS

        with self._lock:
            self._processes[task_id] = process

        try:
            while True:
                if cancel_event.is_set() and process.poll() is None:
                    process.kill()

                now = time.monotonic()
                if not timed_out and now >= deadline and process.poll() is None:
                    timed_out = True
                    process.kill()

                wrote_output = False
                if now >= next_flush:
                    wrote_output = self._flush_stream_output(task_id, stdout_capture, stderr_capture)
                    next_flush = now + LOG_FLUSH_INTERVAL_SECONDS

                if (
                    process.poll() is None
                    and not cancel_event.is_set()
                    and not timed_out
                    and (wrote_output or now >= next_heartbeat)
                ):
                    self.store.update(task_id, status="running")
                    next_heartbeat = now + HEARTBEAT_INTERVAL_SECONDS

                if process.poll() is not None:
                    break

                time.sleep(PROCESS_POLL_INTERVAL_SECONDS)

            process.wait(timeout=STREAM_JOIN_TIMEOUT_SECONDS)
            stdout_capture.closed.wait(STREAM_JOIN_TIMEOUT_SECONDS)
            stderr_capture.closed.wait(STREAM_JOIN_TIMEOUT_SECONDS)
            self._flush_stream_output(task_id, stdout_capture, stderr_capture)
            return stdout_capture.read_all(), stderr_capture.read_all(), timed_out
        finally:
            with self._lock:
                self._processes.pop(task_id, None)

    def _capture_stream(self, stream: TextIO | None, capture: StreamCapture) -> None:
        if stream is None:
            capture.closed.set()
            return
        try:
            for chunk in iter(stream.readline, ""):
                capture.append(chunk)
        finally:
            stream.close()
            capture.closed.set()

    def _flush_stream_output(
        self,
        task_id: str,
        stdout_capture: StreamCapture,
        stderr_capture: StreamCapture,
    ) -> bool:
        stdout = stdout_capture.read_pending()
        stderr = stderr_capture.read_pending()
        if not stdout and not stderr:
            return False
        self.store.append_logs(task_id, stdout=stdout, stderr=stderr)
        return True

    def _available_executors(self) -> list[str]:
        available: list[str] = []
        if _command_available(self.codex_command):
            available.append("codex")
        if _command_available(self.claude_command):
            available.append("claude-code")
        return available

    def _normalize_executor(self, executor: str) -> str | None:
        return EXECUTOR_ALIASES.get((executor or "").strip().lower())

    def _resolve_executor(self, executor: str) -> ResolvedExecutor:
        available = self._available_executors()
        normalized = self._normalize_executor(executor)
        if normalized is None:
            return ResolvedExecutor(
                requested_executor=executor,
                selected_executor=None,
                command=None,
                available_executors=available,
                error=_issue("unsupported_executor", f"Unsupported executor: {executor}"),
            )
        if normalized == "auto":
            if "codex" in available:
                return ResolvedExecutor(executor, "codex", self.codex_command, available)
            if "claude-code" in available:
                return ResolvedExecutor(executor, "claude-code", self.claude_command, available)
            return ResolvedExecutor(
                requested_executor=executor,
                selected_executor=None,
                command=None,
                available_executors=available,
                error=_issue("executor_not_available", "No delegate executor command is available."),
            )
        if normalized == "codex":
            if not _command_available(self.codex_command):
                return ResolvedExecutor(
                    requested_executor=executor,
                    selected_executor=None,
                    command=None,
                    available_executors=available,
                    error=_issue("executor_not_available", "Codex command is not available."),
                )
            return ResolvedExecutor(executor, "codex", self.codex_command, available)
        if not _command_available(self.claude_command):
            return ResolvedExecutor(
                requested_executor=executor,
                selected_executor=None,
                command=None,
                available_executors=available,
                error=_issue("executor_not_available", "Claude Code command is not available."),
            )
        return ResolvedExecutor(executor, "claude-code", self.claude_command, available)

    def _validate_cwd(self, cwd: Path) -> dict[str, object] | None:
        if not cwd.exists():
            return _error_response("cwd_not_found", f"Working directory not found: {cwd}", cwd=str(cwd))
        if not cwd.is_dir():
            return _error_response("cwd_not_directory", f"Working directory is not a directory: {cwd}", cwd=str(cwd))
        return None

    def _resolved_command_string(self, command: str | None) -> str:
        if not command:
            return ""
        parts = _normalized_command_parts(command)
        if parts:
            return _shell_join(parts)
        return command

    def _preview_invocation(self, invocation: Invocation) -> str:
        if isinstance(invocation.args, str):
            return _truncate_text(invocation.args)
        return _truncate_text(_shell_join([_truncate_text(part, 120) for part in invocation.args]), 400)

    def _build_exec_invocation(
        self,
        *,
        executor_name: str,
        command: str,
        cwd: Path,
        task: str | None,
        goal: str | None,
        instructions: str | None,
        context_files: list[str],
        acceptance_criteria: list[str],
        verification_commands: list[str],
        commit_mode: str,
        model: str | None,
    ) -> Invocation:
        prompt = self._build_exec_prompt(
            task=task,
            goal=goal,
            instructions=instructions,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            verification_commands=verification_commands,
            commit_mode=commit_mode,
        )
        if executor_name == "codex":
            parts = _normalized_command_parts(command)
            if parts and _command_name(parts[0]) == "codex":
                args = [*parts, "exec", "--dangerously-bypass-approvals-and-sandbox", "-C", str(cwd)]
                if model:
                    args.extend(["--model", model])
                if not self._is_git_repo(cwd):
                    args.append("--skip-git-repo-check")
                args.append(prompt)
                return Invocation(args=args, use_shell=False, label="codex exec")
        if executor_name == "claude-code":
            parts = _normalized_command_parts(command)
            if parts and _command_name(parts[0]) == "claude":
                args = [
                    *parts,
                    "--print",
                    "--dangerously-skip-permissions",
                    "--permission-mode",
                    "bypassPermissions",
                    "--output-format",
                    "text",
                ]
                if model:
                    args.extend(["--model", model])
                args.append(prompt)
                return Invocation(args=args, use_shell=False, label="claude exec")
        return Invocation(args=command, use_shell=True, label=f"{executor_name} exec")

    def _build_review_invocations(
        self,
        *,
        executor_name: str,
        command: str,
        cwd: Path,
        model: str | None,
        split_strategy: str,
        instructions: str,
        commit: str | None = None,
        base_ref: str | None = None,
        head_ref: str | None = None,
        commits: list[str] | None = None,
        uncommitted: bool = False,
    ) -> list[Invocation]:
        if executor_name == "codex":
            return self._build_codex_review_invocations(
                command=command,
                cwd=cwd,
                model=model,
                split_strategy=split_strategy,
                instructions=instructions,
                commit=commit,
                base_ref=base_ref,
                head_ref=head_ref,
                commits=commits or [],
                uncommitted=uncommitted,
            )
        return self._build_claude_review_invocations(
            command=command,
            cwd=cwd,
            model=model,
            split_strategy=split_strategy,
            instructions=instructions,
            commit=commit,
            base_ref=base_ref,
            head_ref=head_ref,
            commits=commits or [],
            uncommitted=uncommitted,
        )

    def _build_codex_review_invocations(
        self,
        *,
        command: str,
        cwd: Path,
        model: str | None,
        split_strategy: str,
        instructions: str,
        commit: str | None,
        base_ref: str | None,
        head_ref: str | None,
        commits: list[str],
        uncommitted: bool,
    ) -> list[Invocation]:
        parts = _normalized_command_parts(command)
        if not parts or _command_name(parts[0]) != "codex":
            return [Invocation(args=command, use_shell=True, label="codex review")]

        def native_review_args(*review_args: str, prompt: str | None = None) -> list[str]:
            args = [*parts, "exec", "--dangerously-bypass-approvals-and-sandbox", "-C", str(cwd), "review"]
            args.extend(review_args)
            if model:
                args.extend(["--model", model])
            if prompt:
                args.append(prompt)
            return args

        if uncommitted:
            return [Invocation(args=native_review_args("--uncommitted", prompt=instructions), use_shell=False, label="review uncommitted changes")]
        if commit:
            title = self._commit_title(cwd, commit)
            review_args = ["--commit", commit]
            if title:
                review_args.extend(["--title", title])
            return [Invocation(args=native_review_args(*review_args, prompt=instructions), use_shell=False, label=f"review commit {commit[:12]}")]
        assert base_ref is not None
        effective_head_ref = head_ref or "HEAD"
        if split_strategy == "single":
            current_head = self._resolve_commit_ref(cwd, "HEAD")
            target_head = self._resolve_commit_ref(cwd, effective_head_ref)
            if target_head and current_head and target_head == current_head:
                return [Invocation(args=native_review_args("--base", base_ref, prompt=instructions), use_shell=False, label=f"review range {base_ref}..{effective_head_ref}")]
            fallback_prompt = self._build_claude_review_prompt(
                target_description=f"range {base_ref}..{effective_head_ref}",
                inspector_hints=[
                    f"git log --reverse --oneline {base_ref}..{effective_head_ref}",
                    f"git diff --stat {base_ref}..{effective_head_ref}",
                    f"git diff {base_ref}..{effective_head_ref}",
                ],
                additional_instructions=instructions,
            )
            return [
                self._build_exec_invocation(
                    executor_name="codex",
                    command=command,
                    cwd=cwd,
                    task=f"Review the exact git range {base_ref}..{effective_head_ref}. Do not modify files.",
                    goal="Produce a code review report.",
                    instructions=fallback_prompt,
                    context_files=[],
                    acceptance_criteria=[],
                    verification_commands=[],
                    commit_mode="forbidden",
                    model=model,
                )
            ]

        invocations: list[Invocation] = []
        for commit_sha in commits:
            title = self._commit_title(cwd, commit_sha)
            review_args = ["--commit", commit_sha]
            if title:
                review_args.extend(["--title", title])
            invocations.append(
                Invocation(
                    args=native_review_args(*review_args, prompt=instructions),
                    use_shell=False,
                    label=f"review commit {commit_sha[:12]}",
                )
            )
        return invocations

    def _build_claude_review_invocations(
        self,
        *,
        command: str,
        cwd: Path,
        model: str | None,
        split_strategy: str,
        instructions: str,
        commit: str | None,
        base_ref: str | None,
        head_ref: str | None,
        commits: list[str],
        uncommitted: bool,
    ) -> list[Invocation]:
        parts = _normalized_command_parts(command)
        if not parts or _command_name(parts[0]) != "claude":
            return [Invocation(args=command, use_shell=True, label="claude review")]

        def build_claude_invocation(prompt: str, label: str) -> Invocation:
            args = [
                *parts,
                "--print",
                "--dangerously-skip-permissions",
                "--permission-mode",
                "bypassPermissions",
                "--output-format",
                "text",
            ]
            if model:
                args.extend(["--model", model])
            args.append(prompt)
            return Invocation(args=args, use_shell=False, label=label)

        if uncommitted:
            prompt = self._build_claude_review_prompt(
                target_description="uncommitted changes",
                inspector_hints=["git status --short", "git diff --stat", "git diff", "git diff --cached"],
                additional_instructions=instructions,
            )
            return [build_claude_invocation(prompt, "review uncommitted changes")]
        if commit:
            prompt = self._build_claude_review_prompt(
                target_description=f"commit {commit}",
                inspector_hints=[f"git show --stat --patch {commit}"],
                additional_instructions=instructions,
            )
            return [build_claude_invocation(prompt, f"review commit {commit[:12]}")]
        assert base_ref is not None
        effective_head_ref = head_ref or "HEAD"
        if split_strategy == "single":
            prompt = self._build_claude_review_prompt(
                target_description=f"range {base_ref}..{effective_head_ref}",
                inspector_hints=[
                    f"git log --reverse --oneline {base_ref}..{effective_head_ref}",
                    f"git diff --stat {base_ref}..{effective_head_ref}",
                    f"git diff {base_ref}..{effective_head_ref}",
                ],
                additional_instructions=instructions,
            )
            return [build_claude_invocation(prompt, f"review range {base_ref}..{effective_head_ref}")]
        invocations: list[Invocation] = []
        for commit_sha in commits:
            prompt = self._build_claude_review_prompt(
                target_description=f"commit {commit_sha}",
                inspector_hints=[f"git show --stat --patch {commit_sha}"],
                additional_instructions=instructions,
            )
            invocations.append(build_claude_invocation(prompt, f"review commit {commit_sha[:12]}"))
        return invocations

    def _build_exec_prompt(
        self,
        *,
        task: str | None,
        goal: str | None,
        instructions: str | None,
        context_files: list[str],
        acceptance_criteria: list[str],
        verification_commands: list[str],
        commit_mode: str,
    ) -> str:
        return "\n".join(
            self._build_common_sections(
                task=task,
                goal=goal,
                instructions=instructions,
                context_files=context_files,
                acceptance_criteria=acceptance_criteria,
                verification_commands=verification_commands,
                commit_mode=commit_mode,
            )
        )

    def _build_review_instructions(
        self,
        *,
        task: str | None,
        goal: str | None,
        instructions: str | None,
        context_files: list[str],
        acceptance_criteria: list[str],
        verification_commands: list[str],
        commit_mode: str,
    ) -> str:
        lines = [
            "Perform a code review only. Do not modify files.",
            "Focus on correctness, regressions, safety issues, and missing tests.",
            "Report concrete findings with file paths and reasoning.",
            "",
        ]
        lines.extend(
            self._build_common_sections(
                task=task,
                goal=goal,
                instructions=instructions,
                context_files=context_files,
                acceptance_criteria=acceptance_criteria,
                verification_commands=verification_commands,
                commit_mode=commit_mode,
            )
        )
        return "\n".join(lines).strip()

    def _build_claude_review_prompt(
        self,
        *,
        target_description: str,
        inspector_hints: list[str],
        additional_instructions: str,
    ) -> str:
        lines = [
            "Perform a code review only. Do not edit files.",
            f"Review target: {target_description}.",
            "Inspect only the requested diff scope using git commands.",
        ]
        if inspector_hints:
            lines.append("Use commands such as:")
            lines.extend(f"- {hint}" for hint in inspector_hints)
        lines.extend(
            [
                "",
                "Focus on correctness, regressions, security issues, and missing tests.",
                "If there are no blocking findings, say so explicitly.",
                "",
                additional_instructions,
            ]
        )
        return "\n".join(line for line in lines if line is not None).strip()

    def _build_common_sections(
        self,
        *,
        task: str | None,
        goal: str | None,
        instructions: str | None,
        context_files: list[str],
        acceptance_criteria: list[str],
        verification_commands: list[str],
        commit_mode: str,
    ) -> list[str]:
        lines: list[str] = []
        if goal:
            lines.extend(["Goal:", goal, ""])
        if task:
            lines.extend(["Task:", task, ""])
        if instructions:
            lines.extend(["Instructions:", instructions, ""])
        if acceptance_criteria:
            lines.append("Acceptance criteria:")
            lines.extend(f"- {item}" for item in acceptance_criteria)
            lines.append("")
        if verification_commands:
            lines.append("Verification commands:")
            lines.extend(f"- {item}" for item in verification_commands)
            lines.append("")
        lines.append(f"Commit mode: {commit_mode}")
        if context_files:
            lines.extend(["", "Context files:"])
            lines.extend(f"- {path}" for path in context_files)
        return lines

    def _probe_command(
        self,
        command: str,
        args: list[str],
        *,
        cwd: Path | None,
        timeout: int = CHECK_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        command_parts = _normalized_command_parts(command)
        if not command_parts:
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="empty command")
        return subprocess.run(
            [*command_parts, *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    def _run_git(self, cwd: Path, *args: str, timeout: int = CHECK_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    def _is_git_repo(self, cwd: Path) -> bool:
        probe = self._run_git(cwd, "rev-parse", "--is-inside-work-tree")
        return probe.returncode == 0 and probe.stdout.strip().lower() == "true"

    def _resolve_commit_ref(self, cwd: Path, ref: str) -> str | None:
        probe = self._run_git(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if probe.returncode != 0:
            return None
        return probe.stdout.strip() or None

    def _list_commits_in_range(self, cwd: Path, base_ref: str, head_ref: str) -> list[str]:
        probe = self._run_git(cwd, "rev-list", "--reverse", f"{base_ref}..{head_ref}")
        if probe.returncode != 0:
            return []
        return [line.strip() for line in probe.stdout.splitlines() if line.strip()]

    def _commit_title(self, cwd: Path, commit: str) -> str | None:
        probe = self._run_git(cwd, "log", "-1", "--format=%s", commit)
        if probe.returncode != 0:
            return None
        title = probe.stdout.strip()
        return title or None
