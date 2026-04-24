from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from pathlib import PureWindowsPath

from .config import STREAM_OUTPUT_INTERVAL
from .tasks import TaskStore


class _StreamTimeout(Exception):
    """Raised by _stream_process when the deadline is exceeded."""


TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled"}
ALLOWED_COMMIT_MODES = {"allowed", "required", "forbidden"}
IS_WINDOWS = os.name == "nt"


def _split_command(command: str) -> list[str]:
    return shlex.split(command)


def _kill_process(process: subprocess.Popen) -> None:
    """Kill a process, using process-tree kill on Windows."""
    if process.poll() is not None:
        return
    if IS_WINDOWS:
        # shell=True on Windows means cmd.exe is the direct child;
        # taskkill /T /F kills the entire process tree.
        try:
            result = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 or process.poll() is not None:
                return
        except Exception:
            pass  # fall through to regular kill
    try:
        process.kill()
    except ProcessLookupError:
        pass


def _binary_name(binary: str) -> str:
    if IS_WINDOWS:
        return PureWindowsPath(binary).stem.lower()
    return Path(binary).stem.lower()


def _resolve_delegate_command_parts(command: str) -> list[str]:
    parts = _split_command(command)
    if not IS_WINDOWS or not parts:
        return parts
    if _binary_name(parts[0]) not in {"codex", "claude"}:
        return parts
    resolved = shutil.which(parts[0])
    if resolved:
        parts[0] = resolved
    return parts


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def _command_available(command: str | None) -> bool:
    if not command:
        return False
    parts = _split_command(command)
    if not parts:
        return False
    binary = parts[0]
    if Path(binary).exists():
        return True
    return shutil.which(binary) is not None


def _summarize(stdout: str, stderr: str) -> str:
    for candidate in (stdout.strip(), stderr.strip()):
        if candidate:
            return candidate.splitlines()[-1]
    return ""


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_structured_output(text: str) -> object | None:
    """Best-effort JSON extraction from tool output."""
    stripped = (text or "").strip()
    if not stripped:
        return None

    # Prefer the last fenced json block if present.
    matches = _JSON_BLOCK_RE.findall(stripped)
    if matches:
        candidate = matches[-1].strip()
        if candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

    # Fallback: entire payload is JSON.
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _cwd_error(command: str, cwd: Path) -> dict[str, object] | None:
    if not cwd.exists():
        return {
            "success": False,
            "error": {
                "code": "cwd_not_found",
                "message": f"Working directory not found: {cwd}",
            },
            "cwd": str(cwd),
            "command": command,
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
        }
    return None


@dataclass(frozen=True)
class Invocation:
    args: list[str] | str
    use_shell: bool


class ExecutorRegistry:
    def __init__(self, *, store: TaskStore, codex_command: str | None, claude_command: str | None) -> None:
        self.store = store
        self.codex_command = codex_command
        self.claude_command = claude_command
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._completion_events: dict[str, threading.Event] = {}

    def _register_task(self, task_id: str) -> tuple[threading.Event, threading.Event]:
        cancel_event = threading.Event()
        completion_event = threading.Event()
        with self._lock:
            self._cancel_events[task_id] = cancel_event
            self._completion_events[task_id] = completion_event
        return cancel_event, completion_event

    def _mark_completed(self, task_id: str) -> None:
        with self._lock:
            event = self._completion_events.get(task_id)
        if event is not None:
            event.set()

    def submit(
        self,
        *,
        task: str | None,
        goal: str | None = None,
        executor: str,
        cwd: Path,
        timeout: int,
        context_files: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        verification_commands: list[str] | None = None,
        commit_mode: str = "allowed",
        output_schema: dict[str, object] | None = None,
        parse_structured_output: bool = True,
    ) -> dict[str, object]:
        normalized_task = (task or "").strip()
        normalized_goal = (goal or "").strip()
        if not normalized_task and not normalized_goal:
            raise ValueError("delegate_task requires task or goal.")
        if commit_mode not in ALLOWED_COMMIT_MODES:
            raise ValueError(f"Unsupported commit_mode: {commit_mode}")

        chosen_executor, command = self._resolve_executor(executor)
        created = self.store.create(
            task=normalized_task or normalized_goal,
            executor=chosen_executor,
            cwd=str(cwd),
            timeout=timeout,
            context_files=context_files,
            metadata={
                "goal": normalized_goal or None,
                "acceptance_criteria": acceptance_criteria or [],
                "verification_commands": verification_commands or [],
                "commit_mode": commit_mode,
                "output_schema": output_schema or None,
                "parse_structured_output": parse_structured_output,
            },
        )
        cancel_event, _ = self._register_task(created["task_id"])
        thread = threading.Thread(
            target=self._run_task,
            args=(
                created["task_id"],
                chosen_executor,
                command,
                normalized_task or None,
                normalized_goal or None,
                cwd,
                timeout,
                cancel_event,
                context_files or [],
                acceptance_criteria or [],
                verification_commands or [],
                commit_mode,
                output_schema or None,
                parse_structured_output,
            ),
            daemon=True,
        )
        thread.start()
        return {
            "task_id": created["task_id"],
            "executor": chosen_executor,
            "status": created["status"],
        }

    def submit_command(
        self,
        *,
        command: str,
        cwd: Path,
        timeout: int,
    ) -> dict[str, object]:
        cwd_error = _cwd_error(command, cwd)
        if cwd_error:
            return cwd_error
        created = self.store.create(
            task=command,
            executor="shell",
            cwd=str(cwd),
            timeout=timeout,
            context_files=[],
        )
        cancel_event, _ = self._register_task(created["task_id"])
        thread = threading.Thread(
            target=self._run_command_task,
            args=(created["task_id"], command, cwd, timeout, cancel_event),
            daemon=True,
        )
        thread.start()
        return {
            "task_id": created["task_id"],
            "executor": "shell",
            "status": created["status"],
        }

    def get(self, task_id: str) -> dict[str, object]:
        meta = self.store.get(task_id)
        meta["summary"] = self.store.read_summary(task_id)
        meta["stdout_tail"] = self.store.read_stdout(task_id)[-4000:]
        meta["stderr_tail"] = self.store.read_stderr(task_id)[-4000:]
        meta["artifacts"] = []
        meta["completed"] = meta["status"] in TERMINAL_TASK_STATUSES
        return meta

    def wait(self, task_id: str, timeout: float, poll_interval: float = 0.5) -> dict[str, object]:
        """Block until the task reaches a terminal status or ``timeout`` elapses.

        Event-driven when the task was submitted through this registry instance
        (uses :class:`threading.Event` so there is no wakeup latency). For tasks
        loaded from disk after a server restart the completion event is not
        registered, so we fall back to polling ``meta['completed']`` at
        ``poll_interval`` seconds until the deadline.
        """
        # Fast path: already finished.
        meta = self.get(task_id)
        if meta["completed"]:
            meta["timed_out"] = False
            return meta

        with self._lock:
            completion_event = self._completion_events.get(task_id)

        remaining = max(float(timeout), 0.0)
        if completion_event is not None:
            # Event-driven wait: returns as soon as the worker thread marks
            # completion, or after ``remaining`` seconds, whichever comes first.
            completion_event.wait(timeout=remaining)
            meta = self.get(task_id)
            meta["timed_out"] = not meta["completed"]
            return meta

        # Fallback: no registered event (task persisted from a previous run).
        deadline = time.monotonic() + remaining
        interval = max(float(poll_interval), 0.05)
        while True:
            meta = self.get(task_id)
            if meta["completed"]:
                meta["timed_out"] = False
                return meta
            if time.monotonic() >= deadline:
                meta["timed_out"] = True
                return meta
            time.sleep(interval)

    def cancel(self, task_id: str) -> dict[str, object]:
        try:
            meta = self.store.get(task_id)
        except (FileNotFoundError, ValueError):
            return {
                "success": False,
                "task_id": task_id,
                "error": {
                    "code": "task_not_found",
                    "message": f"Task not found: {task_id}",
                },
                "cancelled": False,
            }

        current_status = meta.get("status", "")
        if current_status in TERMINAL_TASK_STATUSES:
            return {
                "task_id": task_id,
                "status": current_status,
                "cancelled": current_status == "cancelled",
            }

        with self._lock:
            cancel_event = self._cancel_events.get(task_id)
            process = self._processes.get(task_id)
        if cancel_event is not None:
            cancel_event.set()
        if process is not None and process.poll() is None:
            _kill_process(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.store.update(task_id, status="cancelled")
        self._mark_completed(task_id)
        return {
            "task_id": task_id,
            "status": "cancelled",
            "cancelled": True,
        }

    def _resolve_executor(self, executor: str) -> tuple[str, str]:
        if executor == "codex":
            if not _command_available(self.codex_command):
                raise RuntimeError("Codex command is not available.")
            return "codex", self.codex_command or ""
        if executor == "claude-code":
            if not _command_available(self.claude_command):
                raise RuntimeError("Claude Code command is not available.")
            return "claude-code", self.claude_command or ""
        if _command_available(self.codex_command):
            return "codex", self.codex_command or ""
        if _command_available(self.claude_command):
            return "claude-code", self.claude_command or ""
        raise RuntimeError("No delegate executor command is available.")

    def _run_task(
        self,
        task_id: str,
        executor_name: str,
        command: str,
        task: str | None,
        goal: str | None,
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event,
        context_files: list[str],
        acceptance_criteria: list[str],
        verification_commands: list[str],
        commit_mode: str,
        output_schema: dict[str, object] | None,
        parse_structured_output: bool,
    ) -> None:
        try:
            self._run_task_impl(
                task_id,
                executor_name,
                command,
                task,
                goal,
                cwd,
                timeout,
                cancel_event,
                context_files,
                acceptance_criteria,
                verification_commands,
                commit_mode,
                output_schema,
                parse_structured_output,
            )
        finally:
            self._mark_completed(task_id)

    def _stream_process(
        self,
        task_id: str,
        process: subprocess.Popen,
        timeout: int,
        cancel_event: threading.Event,
    ) -> tuple[str, str]:
        """Read process output incrementally, flushing to store periodically.

        Returns (stdout, stderr) as decoded strings.
        Raises _StreamTimeout if the deadline is exceeded.
        """
        import codecs

        stdout_buf = bytearray()
        stderr_buf = bytearray()
        # Track how many bytes have already been flushed to disk so we only
        # append the new delta each interval — avoids O(n²) full rewrites.
        flushed_out = 0
        flushed_err = 0
        buf_lock = threading.Lock()

        # Incremental decoders preserve multi-byte UTF-8 characters that span
        # two flush boundaries (e.g. Chinese/emoji split across chunks).
        out_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        err_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def _reader(stream: object, buf: bytearray) -> None:
            # Use read1() when available (BufferedReader) so we get data as
            # soon as it arrives instead of blocking until the full 4096-byte
            # buffer fills — critical for incremental streaming on Windows.
            _read = getattr(stream, "read1", None) or stream.read  # type: ignore[union-attr]
            while True:
                chunk = _read(4096)
                if not chunk:
                    break
                with buf_lock:
                    buf.extend(chunk)

        t_out = threading.Thread(target=_reader, args=(process.stdout, stdout_buf), daemon=True)
        t_err = threading.Thread(target=_reader, args=(process.stderr, stderr_buf), daemon=True)
        t_out.start()
        t_err.start()

        deadline = time.monotonic() + timeout
        timed_out = False
        interval = STREAM_OUTPUT_INTERVAL
        # Use a short poll tick so process exit is detected quickly;
        # only flush to disk every `interval` seconds.
        poll_tick = min(0.05, interval)
        last_flush = time.monotonic()

        def _flush_incremental(*, final: bool = False) -> None:
            nonlocal flushed_out, flushed_err
            with buf_lock:
                out_bytes = bytes(stdout_buf[flushed_out:])
                err_bytes = bytes(stderr_buf[flushed_err:])
                flushed_out = len(stdout_buf)
                flushed_err = len(stderr_buf)

            new_out = out_decoder.decode(out_bytes, final=final)
            new_err = err_decoder.decode(err_bytes, final=final)

            if new_out:
                stdout_parts.append(new_out)
            if new_err:
                stderr_parts.append(new_err)

            if new_out or new_err:
                self.store.append_logs(task_id, stdout=new_out, stderr=new_err)

            self.store.write_summary(task_id, _summarize("".join(stdout_parts), "".join(stderr_parts)))

        while process.poll() is None:
            if cancel_event.is_set():
                _kill_process(process)
                break
            if time.monotonic() >= deadline:
                _kill_process(process)
                timed_out = True
                break
            now = time.monotonic()
            if now - last_flush >= interval:
                _flush_incremental()
                last_flush = now
            time.sleep(poll_tick)

        # Reap the child properly after kill
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

        # Wait for reader threads to drain remaining data
        t_out.join(timeout=5)
        t_err.join(timeout=5)

        # Final flush — append any remaining bytes, finalize decoders
        _flush_incremental(final=True)

        with self._lock:
            self._processes.pop(task_id, None)

        if timed_out:
            self.store.update(task_id, status="failed", timed_out=True)
            raise _StreamTimeout()

        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        return stdout, stderr

    def _run_task_impl(
        self,
        task_id: str,
        executor_name: str,
        command: str,
        task: str | None,
        goal: str | None,
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event,
        context_files: list[str],
        acceptance_criteria: list[str],
        verification_commands: list[str],
        commit_mode: str,
        output_schema: dict[str, object] | None,
        parse_structured_output: bool,
    ) -> None:
        if cancel_event.is_set():
            self.store.update(task_id, status="cancelled")
            return

        self.store.update(task_id, status="running")
        invocation = self._build_invocation(
            executor_name=executor_name,
            command=command,
            task=task,
            goal=goal,
            cwd=cwd,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            verification_commands=verification_commands,
            commit_mode=commit_mode,
        )
        try:
            process = subprocess.Popen(
                invocation.args,
                cwd=str(cwd),
                shell=invocation.use_shell,
                text=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self.store.write_logs(task_id, stdout="", stderr=str(exc))
            self.store.write_summary(task_id, str(exc))
            self.store.update(task_id, status="failed", exit_code=-1)
            return
        with self._lock:
            self._processes[task_id] = process

        if cancel_event.is_set() and process.poll() is None:
            _kill_process(process)

        try:
            stdout, stderr = self._stream_process(task_id, process, timeout, cancel_event)
        except _StreamTimeout:
            return

        structured_output = None
        if parse_structured_output:
            structured_output = _extract_structured_output(stdout) or _extract_structured_output(stderr)

        if cancel_event.is_set() or self.store.get(task_id)["status"] == "cancelled":
            self.store.update(task_id, status="cancelled")
            return

        status = "succeeded" if process.returncode == 0 else "failed"
        self.store.update(
            task_id,
            status=status,
            exit_code=process.returncode,
            structured_output=structured_output,
            output_schema=output_schema or None,
        )

    def _run_command_task(
        self,
        task_id: str,
        command: str,
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event,
    ) -> None:
        try:
            self._run_command_task_impl(task_id, command, cwd, timeout, cancel_event)
        finally:
            self._mark_completed(task_id)

    def _run_command_task_impl(
        self,
        task_id: str,
        command: str,
        cwd: Path,
        timeout: int,
        cancel_event: threading.Event,
    ) -> None:
        if cancel_event.is_set():
            self.store.update(task_id, status="cancelled")
            return

        self.store.update(task_id, status="running")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                shell=True,
                text=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self.store.write_logs(task_id, stdout="", stderr=str(exc))
            self.store.write_summary(task_id, str(exc))
            self.store.update(task_id, status="failed")
            return
        with self._lock:
            self._processes[task_id] = process

        if cancel_event.is_set() and process.poll() is None:
            _kill_process(process)

        try:
            stdout, stderr = self._stream_process(task_id, process, timeout, cancel_event)
        except _StreamTimeout:
            return

        structured_output = _extract_structured_output(stdout) or _extract_structured_output(stderr)

        if cancel_event.is_set() or self.store.get(task_id)["status"] == "cancelled":
            self.store.update(task_id, status="cancelled")
            return

        status = "succeeded" if process.returncode == 0 else "failed"
        self.store.update(task_id, status=status, exit_code=process.returncode, structured_output=structured_output)

    def _build_invocation(
        self,
        *,
        executor_name: str,
        command: str,
        task: str | None,
        goal: str | None,
        cwd: Path,
        context_files: list[str],
        acceptance_criteria: list[str],
        verification_commands: list[str],
        commit_mode: str,
    ) -> Invocation:
        prompt = self._build_prompt(
            task=task,
            goal=goal,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            verification_commands=verification_commands,
            commit_mode=commit_mode,
        )
        if executor_name == "codex":
            parts = _resolve_delegate_command_parts(command)
            if _binary_name(parts[0]) == "codex":
                args = [
                    *parts,
                    "exec",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-C",
                    str(cwd),
                ]
                if not (cwd / ".git").exists():
                    args.append("--skip-git-repo-check")
                args.append(prompt)
                return Invocation(args=args, use_shell=False)
        if executor_name == "claude-code":
            parts = _resolve_delegate_command_parts(command)
            if _binary_name(parts[0]) == "claude":
                return Invocation(
                    args=[
                        *parts,
                        "--print",
                        "--dangerously-skip-permissions",
                        "--permission-mode",
                        "bypassPermissions",
                        "--output-format",
                        "text",
                        prompt,
                    ],
                    use_shell=False,
                )
        return Invocation(args=command, use_shell=True)

    def _build_prompt(
        self,
        *,
        task: str | None,
        goal: str | None,
        context_files: list[str],
        acceptance_criteria: list[str],
        verification_commands: list[str],
        commit_mode: str,
    ) -> str:
        lines: list[str] = []
        if goal:
            lines.extend(["Goal:", goal, ""])
        if task:
            lines.extend(["Task:", task, ""])
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
        return "\n".join(lines)
