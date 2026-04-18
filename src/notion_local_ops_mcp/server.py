from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware
import uvicorn

from .config import (
    APP_NAME,
    AUTH_TOKEN,
    CLAUDE_COMMAND,
    CODEX_COMMAND,
    COMMAND_TIMEOUT,
    DELEGATE_TIMEOUT,
    HOST,
    PORT,
    STATE_DIR,
    WORKSPACE_ROOT,
    ensure_runtime_directories,
)
from .executors import ExecutorRegistry
from .files import list_files as list_files_impl
from .files import read_file as read_file_impl
from .files import read_files as read_files_impl
from .files import replace_in_file as replace_in_file_impl
from .files import write_file as write_file_impl
from .gitops import git_commit as git_commit_impl
from .gitops import git_diff as git_diff_impl
from .gitops import git_log as git_log_impl
from .gitops import git_status as git_status_impl
from .patching import apply_patch as apply_patch_impl
from .pathing import resolve_cwd, resolve_path
from .search import glob_files as glob_files_impl
from .search import grep_files as grep_files_impl
from .search import search_files as search_files_impl
from .shell import run_command as run_command_impl
from .tasks import TaskStore


def _extract_bearer_token(headers: dict[str, str]) -> str:
    authorization = headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


class BearerAuthMiddleware(Middleware):
    async def on_request(self, context: Any, call_next: Any) -> Any:
        if not AUTH_TOKEN:
            return await call_next(context)
        request = get_http_request()
        headers = {str(key).lower(): str(value) for key, value in request.headers.items()}
        token = _extract_bearer_token(headers)
        if token != AUTH_TOKEN:
            raise AuthorizationError("Unauthorized: invalid bearer token.")
        return await call_next(context)


store = TaskStore(STATE_DIR)
registry = ExecutorRegistry(
    store=store,
    codex_command=CODEX_COMMAND,
    claude_command=CLAUDE_COMMAND,
)

mcp = FastMCP(
    APP_NAME,
    instructions=(
        "Use direct tools first for normal tasks: list/glob/grep/read/replace/write/patch/git/run. "
        "Prefer explicit delegate tools for executor work: delegate_doctor, codex_exec, claude_exec, "
        "claudecode_exec, codex_review, claude_review, and list_tasks. "
        "Keep delegate_task only as the compatibility fallback."
    ),
    middleware=[BearerAuthMiddleware()],
)


@mcp.tool(
    name="list_files",
    description="List files and directories. Use this before reading or editing when you need folder context.",
)
def list_files(
    path: str | None = None,
    recursive: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, object]:
    target = resolve_path(path or ".", WORKSPACE_ROOT)
    return list_files_impl(target, recursive=recursive, limit=limit, offset=offset)


@mcp.tool(
    name="search_files",
    description="Simple substring search in files. Prefer grep_files for regex, context lines, or advanced filtering.",
)
def search_files(
    query: str,
    path: str | None = None,
    glob: str | None = None,
    limit: int = 100,
) -> dict[str, object]:
    target = resolve_path(path or ".", WORKSPACE_ROOT)
    return search_files_impl(target, query=query, glob_pattern=glob, limit=limit)


@mcp.tool(
    name="glob_files",
    description="Find files or directories by glob pattern. Use this to narrow candidate paths before reading or editing.",
)
def glob_files(
    pattern: str,
    path: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, object]:
    target = resolve_path(path or ".", WORKSPACE_ROOT)
    return glob_files_impl(target, pattern=pattern, limit=limit, offset=offset)


@mcp.tool(
    name="grep_files",
    description="Advanced regex search across files with glob filtering, context lines, and alternate output modes.",
)
def grep_files(
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    output_mode: str = "content",
    before: int = 0,
    after: int = 0,
    ignore_case: bool = False,
    limit: int = 200,
    offset: int = 0,
    multiline: bool = False,
) -> dict[str, object]:
    target = resolve_path(path or ".", WORKSPACE_ROOT)
    return grep_files_impl(
        target,
        pattern=pattern,
        glob_pattern=glob,
        output_mode=output_mode,
        before=before,
        after=after,
        ignore_case=ignore_case,
        head_limit=limit,
        offset=offset,
        multiline=multiline,
    )


@mcp.tool(
    name="read_file",
    description="Read a text file with optional offset and limit.",
)
def read_file(path: str, offset: int | None = None, limit: int | None = None) -> dict[str, object]:
    target = resolve_path(path, WORKSPACE_ROOT)
    return read_file_impl(target, offset=offset, limit=limit, max_lines=200, max_bytes=32768)


@mcp.tool(
    name="read_files",
    description="Read multiple text files with the same optional offset and limit.",
)
def read_files(
    paths: list[str],
    offset: int | None = None,
    limit: int | None = None,
) -> dict[str, object]:
    targets = [resolve_path(path, WORKSPACE_ROOT) for path in paths]
    return read_files_impl(targets, offset=offset, limit=limit, max_lines=200, max_bytes=32768)


@mcp.tool(
    name="replace_in_file",
    description="Replace an exact text fragment in a file. Can replace one unique match or all matches.",
)
def replace_in_file(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> dict[str, object]:
    target = resolve_path(path, WORKSPACE_ROOT)
    return replace_in_file_impl(target, old_text=old_text, new_text=new_text, replace_all=replace_all)


@mcp.tool(
    name="write_file",
    description="Write full content to a file, creating parent directories when needed.",
)
def write_file(path: str, content: str) -> dict[str, object]:
    target = resolve_path(path, WORKSPACE_ROOT)
    return write_file_impl(target, content=content)


@mcp.tool(
    name="apply_patch",
    description="Apply a codex-style patch with add, update, move, or delete operations.",
)
def apply_patch(
    patch: str,
    dry_run: bool = False,
    validate_only: bool = False,
    return_diff: bool = False,
) -> dict[str, object]:
    return apply_patch_impl(
        patch=patch,
        workspace_root=WORKSPACE_ROOT,
        dry_run=dry_run,
        validate_only=validate_only,
        return_diff=return_diff,
    )


@mcp.tool(
    name="git_status",
    description="Return structured git status for the repository at cwd or the current workspace root.",
)
def git_status(cwd: str | None = None) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_status_impl(cwd=resolved_cwd)


@mcp.tool(
    name="git_diff",
    description="Return git diff output and changed file paths for the repository at cwd.",
)
def git_diff(
    cwd: str | None = None,
    staged: bool = False,
    paths: list[str] | None = None,
    max_bytes: int = 65536,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_diff_impl(cwd=resolved_cwd, staged=staged, paths=paths, max_bytes=max_bytes)


@mcp.tool(
    name="git_commit",
    description="Create a git commit for staged changes, selected paths, or all current changes.",
)
def git_commit(
    message: str,
    cwd: str | None = None,
    paths: list[str] | None = None,
    stage_all: bool = False,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_commit_impl(cwd=resolved_cwd, message=message, paths=paths, stage_all=stage_all)


@mcp.tool(
    name="git_log",
    description="Return recent git commits for the repository at cwd.",
)
def git_log(cwd: str | None = None, limit: int = 10) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return git_log_impl(cwd=resolved_cwd, limit=limit)


@mcp.tool(
    name="run_command",
    description="Run a local shell command now or queue it as a background task for wait_task/get_task polling.",
)
def run_command(
    command: str,
    cwd: str | None = None,
    timeout: int | None = None,
    run_in_background: bool = False,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    effective_timeout = timeout if timeout is not None else COMMAND_TIMEOUT
    if run_in_background:
        return registry.submit_command(
            command=command,
            cwd=resolved_cwd,
            timeout=effective_timeout,
        )
    return run_command_impl(
        command=command,
        cwd=resolved_cwd,
        timeout=effective_timeout,
    )


@mcp.tool(
    name="delegate_doctor",
    description="Run local preflight checks for codex/claude executor health, git state, and MCP auth warnings.",
)
def delegate_doctor(
    cwd: str | None = None,
    executor: str = "auto",
    check_git: bool = True,
    check_mcp_auth: bool = True,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return registry.doctor(
        cwd=resolved_cwd,
        executor=executor,
        check_git=check_git,
        check_mcp_auth=check_mcp_auth,
    )


def _submit_exec_tool(
    *,
    executor: str,
    task: str | None,
    goal: str | None,
    instructions: str | None,
    cwd: str | None,
    context_files: list[str] | None,
    acceptance_criteria: list[str] | None,
    verification_commands: list[str] | None,
    commit_mode: str,
    timeout: int | None,
    model: str | None,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return registry.submit(
        task=task,
        goal=goal,
        instructions=instructions,
        executor=executor,
        cwd=resolved_cwd,
        timeout=timeout if timeout is not None else DELEGATE_TIMEOUT,
        context_files=context_files,
        acceptance_criteria=acceptance_criteria,
        verification_commands=verification_commands,
        commit_mode=commit_mode,
        model=model,
    )


def _submit_review_tool(
    *,
    executor: str,
    task: str | None,
    goal: str | None,
    instructions: str | None,
    cwd: str | None,
    context_files: list[str] | None,
    acceptance_criteria: list[str] | None,
    verification_commands: list[str] | None,
    commit_mode: str,
    timeout: int | None,
    model: str | None,
    commit: str | None,
    base_ref: str | None,
    head_ref: str | None,
    uncommitted: bool,
    split_strategy: str,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return registry.submit_review(
        executor=executor,
        cwd=resolved_cwd,
        timeout=timeout if timeout is not None else DELEGATE_TIMEOUT,
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


@mcp.tool(
    name="codex_exec",
    description="Queue a long-running codex task with explicit schema and structured task metadata.",
)
def codex_exec(
    task: str | None = None,
    goal: str | None = None,
    instructions: str | None = None,
    cwd: str | None = None,
    context_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    verification_commands: list[str] | None = None,
    commit_mode: str = "allowed",
    timeout: int | None = None,
    model: str | None = None,
) -> dict[str, object]:
    return _submit_exec_tool(
        executor="codex",
        task=task,
        goal=goal,
        instructions=instructions,
        cwd=cwd,
        context_files=context_files,
        acceptance_criteria=acceptance_criteria,
        verification_commands=verification_commands,
        commit_mode=commit_mode,
        timeout=timeout,
        model=model,
    )


@mcp.tool(
    name="claude_exec",
    description="Queue a long-running Claude Code task with explicit schema and structured task metadata.",
)
def claude_exec(
    task: str | None = None,
    goal: str | None = None,
    instructions: str | None = None,
    cwd: str | None = None,
    context_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    verification_commands: list[str] | None = None,
    commit_mode: str = "allowed",
    timeout: int | None = None,
    model: str | None = None,
) -> dict[str, object]:
    return _submit_exec_tool(
        executor="claude-code",
        task=task,
        goal=goal,
        instructions=instructions,
        cwd=cwd,
        context_files=context_files,
        acceptance_criteria=acceptance_criteria,
        verification_commands=verification_commands,
        commit_mode=commit_mode,
        timeout=timeout,
        model=model,
    )


@mcp.tool(
    name="claudecode_exec",
    description="Compatibility alias for claude_exec using the legacy claudecode naming.",
)
def claudecode_exec(
    task: str | None = None,
    goal: str | None = None,
    instructions: str | None = None,
    cwd: str | None = None,
    context_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    verification_commands: list[str] | None = None,
    commit_mode: str = "allowed",
    timeout: int | None = None,
    model: str | None = None,
) -> dict[str, object]:
    return claude_exec(
        task=task,
        goal=goal,
        instructions=instructions,
        cwd=cwd,
        context_files=context_files,
        acceptance_criteria=acceptance_criteria,
        verification_commands=verification_commands,
        commit_mode=commit_mode,
        timeout=timeout,
        model=model,
    )


@mcp.tool(
    name="codex_review",
    description="Queue a codex-native code review. Commit ranges default to per-commit slicing.",
)
def codex_review(
    task: str | None = None,
    goal: str | None = None,
    instructions: str | None = None,
    cwd: str | None = None,
    context_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    verification_commands: list[str] | None = None,
    commit_mode: str = "forbidden",
    timeout: int | None = None,
    model: str | None = None,
    commit: str | None = None,
    base_ref: str | None = None,
    head_ref: str | None = None,
    uncommitted: bool = False,
    split_strategy: str = "by_commit",
) -> dict[str, object]:
    return _submit_review_tool(
        executor="codex",
        task=task,
        goal=goal,
        instructions=instructions,
        cwd=cwd,
        context_files=context_files,
        acceptance_criteria=acceptance_criteria,
        verification_commands=verification_commands,
        commit_mode=commit_mode,
        timeout=timeout,
        model=model,
        commit=commit,
        base_ref=base_ref,
        head_ref=head_ref,
        uncommitted=uncommitted,
        split_strategy=split_strategy,
    )


@mcp.tool(
    name="claude_review",
    description="Queue a Claude Code review. Commit ranges default to per-commit slicing.",
)
def claude_review(
    task: str | None = None,
    goal: str | None = None,
    instructions: str | None = None,
    cwd: str | None = None,
    context_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    verification_commands: list[str] | None = None,
    commit_mode: str = "forbidden",
    timeout: int | None = None,
    model: str | None = None,
    commit: str | None = None,
    base_ref: str | None = None,
    head_ref: str | None = None,
    uncommitted: bool = False,
    split_strategy: str = "by_commit",
) -> dict[str, object]:
    return _submit_review_tool(
        executor="claude-code",
        task=task,
        goal=goal,
        instructions=instructions,
        cwd=cwd,
        context_files=context_files,
        acceptance_criteria=acceptance_criteria,
        verification_commands=verification_commands,
        commit_mode=commit_mode,
        timeout=timeout,
        model=model,
        commit=commit,
        base_ref=base_ref,
        head_ref=head_ref,
        uncommitted=uncommitted,
        split_strategy=split_strategy,
    )


@mcp.tool(
    name="delegate_task",
    description=(
        "Fallback only. Use this when direct tools are insufficient for a complex, long-running, or "
        "multi-file task. Supported executors: auto, codex, claude, claudecode, claude-code."
    ),
)
def delegate_task(
    task: str | None = None,
    goal: str | None = None,
    instructions: str | None = None,
    executor: str = "auto",
    cwd: str | None = None,
    context_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    verification_commands: list[str] | None = None,
    commit_mode: str = "allowed",
    timeout: int | None = None,
    model: str | None = None,
) -> dict[str, object]:
    return _submit_exec_tool(
        executor=executor,
        task=task,
        goal=goal,
        instructions=instructions,
        cwd=cwd,
        context_files=context_files,
        acceptance_criteria=acceptance_criteria,
        verification_commands=verification_commands,
        commit_mode=commit_mode,
        timeout=timeout,
        model=model,
    )


@mcp.tool(
    name="get_task",
    description="Get the current status and output tail for a delegated or background shell task.",
)
def get_task(task_id: str) -> dict[str, object]:
    return registry.get(task_id)


@mcp.tool(
    name="list_tasks",
    description="List recent delegated or background shell tasks with optional status/executor filters.",
)
def list_tasks(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    executor: str | None = None,
) -> dict[str, object]:
    return registry.list_tasks(limit=limit, offset=offset, status=status, executor=executor)


@mcp.tool(
    name="wait_task",
    description="Wait for a delegated or background shell task to finish or until timeout, then return its latest status and output tail.",
)
def wait_task(task_id: str, timeout: float = 30, poll_interval: float = 0.5) -> dict[str, object]:
    return registry.wait(task_id, timeout=timeout, poll_interval=poll_interval)


@mcp.tool(
    name="cancel_task",
    description="Cancel a delegated or background shell task if it is still running.",
)
def cancel_task(task_id: str) -> dict[str, object]:
    return registry.cancel(task_id)


def build_http_app():
    return mcp.http_app(
        path="/mcp",
        transport="http",
    )


def main() -> None:
    ensure_runtime_directories()
    print(f"Starting {APP_NAME} on {HOST}:{PORT}")
    print(f"workspace_root={WORKSPACE_ROOT}")
    print(f"state_dir={STATE_DIR}")
    print("mcp_path=/mcp")
    print("transport=streamable-http")
    app = build_http_app()
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
