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
from .skills import list_skills as list_skills_impl
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
        "Use delegate_task only when direct tools are insufficient for a complex, long-running, or multi-file task."
    ),
    middleware=[BearerAuthMiddleware()],
)


@mcp.tool(
    name="list_skills",
    description=(
        "List project and global agent skills as lightweight summaries. "
        "Returns skill name, description, preferred path, and source locations."
    ),
)
def list_skills(
    include_project: bool = True,
    include_global: bool = True,
) -> dict[str, object]:
    return list_skills_impl(
        workspace_root=WORKSPACE_ROOT,
        include_project=include_project,
        include_global=include_global,
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
    name="delegate_task",
    description=(
        "Fallback only. Use this when direct tools are insufficient for a complex, long-running, or "
        "multi-file task. Supported executors: auto, codex, claude-code."
    ),
)
def delegate_task(
    task: str | None = None,
    goal: str | None = None,
    executor: str = "auto",
    cwd: str | None = None,
    context_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    verification_commands: list[str] | None = None,
    commit_mode: str = "allowed",
    timeout: int | None = None,
) -> dict[str, object]:
    resolved_cwd = resolve_cwd(cwd, WORKSPACE_ROOT)
    return registry.submit(
        task=task,
        goal=goal,
        executor=executor,
        cwd=resolved_cwd,
        timeout=timeout if timeout is not None else DELEGATE_TIMEOUT,
        context_files=context_files,
        acceptance_criteria=acceptance_criteria,
        verification_commands=verification_commands,
        commit_mode=commit_mode,
    )


@mcp.tool(
    name="get_task",
    description="Get the current status and output tail for a delegated or background shell task.",
)
def get_task(task_id: str) -> dict[str, object]:
    return registry.get(task_id)


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
        transport="sse",
    )


def main() -> None:
    ensure_runtime_directories()
    print(f"Starting {APP_NAME} on {HOST}:{PORT}")
    print(f"workspace_root={WORKSPACE_ROOT}")
    print(f"state_dir={STATE_DIR}")
    print("sse_path=/mcp")
    print("message_path=/messages/")
    app = build_http_app()
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
