from __future__ import annotations

import subprocess
from pathlib import Path


def _error(code: str, message: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    payload.update(extra)
    return payload


def _cwd_error(cwd: Path) -> dict[str, object] | None:
    if not cwd.exists():
        return _error("cwd_not_found", f"Working directory not found: {cwd}", cwd=str(cwd))
    if not cwd.is_dir():
        return _error("cwd_not_directory", f"Working directory is not a directory: {cwd}", cwd=str(cwd))
    return None


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )


def _require_repo(cwd: Path) -> tuple[Path, str] | dict[str, object]:
    cwd_error = _cwd_error(cwd)
    if cwd_error:
        return cwd_error

    root_result = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if root_result.returncode != 0:
        return _error(
            "not_a_git_repo",
            "Working directory is not inside a git repository.",
            cwd=str(cwd),
            stderr=root_result.stderr.strip(),
        )

    branch_result = _run_git(["branch", "--show-current"], cwd=cwd)
    branch = branch_result.stdout.strip() or "HEAD"
    return Path(root_result.stdout.strip()), branch


def _normalize_pathspec(pathspec: str, *, cwd: Path, repo_root: Path) -> str:
    raw = Path(pathspec).expanduser()
    absolute = (cwd / raw).resolve(strict=False) if not raw.is_absolute() else raw.resolve(strict=False)
    try:
        return str(absolute.relative_to(repo_root))
    except ValueError:
        return pathspec


def git_status(*, cwd: Path) -> dict[str, object]:
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, branch = repo_info

    result = _run_git(["status", "--short", "--branch"], cwd=cwd)
    if result.returncode != 0:
        return _error("git_status_failed", result.stderr.strip() or "git status failed.", cwd=str(cwd))

    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    entries: list[dict[str, object]] = []

    for line in result.stdout.splitlines():
        if line.startswith("## "):
            continue
        code = line[:2]
        raw_path = line[3:]
        path = raw_path.split(" -> ", 1)[-1]
        entries.append(
            {
                "path": path,
                "index_status": code[0],
                "worktree_status": code[1],
            }
        )
        if code == "??":
            untracked.append(path)
            continue
        if code[0] != " ":
            staged.append(path)
        if code[1] != " ":
            unstaged.append(path)

    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "branch": branch,
        "clean": not entries,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "entries": entries,
    }


_DIFF_FILE_HEADER = "diff --git "


def _split_diff_by_file(diff_text: str) -> list[tuple[str, str]]:
    """Split a combined unified diff into ``(path, per_file_diff)`` tuples.

    The path is derived from the ``+++ b/<path>`` marker when present; falling
    back to the ``diff --git`` header. Binary-file stanzas and deletions still
    work because we lean on the git-provided headers.
    """
    if not diff_text:
        return []
    chunks: list[tuple[str, str]] = []
    buffer: list[str] = []
    current_path = ""

    def flush() -> None:
        if not buffer:
            return
        chunks.append((current_path or "(unknown)", "".join(buffer)))
        buffer.clear()

    for raw_line in diff_text.splitlines(keepends=True):
        if raw_line.startswith(_DIFF_FILE_HEADER):
            flush()
            header = raw_line[len(_DIFF_FILE_HEADER) :].rstrip("\n")
            parts = header.split(" ")
            # headers look like `a/<path> b/<path>`; fall back to last token.
            candidate = parts[-1] if parts else ""
            current_path = candidate[2:] if candidate.startswith("b/") else candidate
        elif raw_line.startswith("+++ ") and buffer:
            marker = raw_line[4:].rstrip("\n")
            if marker.startswith("b/"):
                current_path = marker[2:]
            elif marker != "/dev/null":
                current_path = marker
        buffer.append(raw_line)
    flush()
    return chunks


def git_diff(
    *,
    cwd: Path,
    staged: bool = False,
    paths: list[str] | None = None,
    max_bytes: int = 65536,
    per_file_max_bytes: int = 16384,
) -> dict[str, object]:
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, _branch = repo_info
    normalized_paths = [_normalize_pathspec(path, cwd=cwd, repo_root=repo_root) for path in (paths or [])]

    args = ["diff", "--no-color"]
    if staged:
        args.append("--cached")
    if normalized_paths:
        args.extend(["--", *normalized_paths])

    result = _run_git(args, cwd=cwd)
    if result.returncode != 0:
        return _error("git_diff_failed", result.stderr.strip() or "git diff failed.", cwd=str(cwd))

    # numstat gives added/removed line counts per file (binary => "-").
    numstat_args = ["diff", "--numstat"]
    if staged:
        numstat_args.append("--cached")
    if normalized_paths:
        numstat_args.extend(["--", *normalized_paths])
    numstat = _run_git(numstat_args, cwd=cwd)
    stats: dict[str, dict[str, object]] = {}
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_raw, removed_raw, path = parts
        stats[path] = {
            "added": None if added_raw == "-" else int(added_raw),
            "removed": None if removed_raw == "-" else int(removed_raw),
            "binary": added_raw == "-" and removed_raw == "-",
        }

    files_payload: list[dict[str, object]] = []
    for path, chunk in _split_diff_by_file(result.stdout):
        encoded_chunk = chunk.encode("utf-8")
        chunk_truncated = len(encoded_chunk) > per_file_max_bytes
        rendered = (
            encoded_chunk[:per_file_max_bytes].decode("utf-8", errors="ignore")
            if chunk_truncated
            else chunk
        )
        files_payload.append(
            {
                "path": path,
                "diff": rendered,
                "truncated": chunk_truncated,
                "bytes": len(encoded_chunk),
                **stats.get(path, {"added": None, "removed": None, "binary": False}),
            }
        )

    # Keep the flat `diff` + `files` names for back-compat; add richer payload.
    encoded = result.stdout.encode("utf-8")
    truncated = len(encoded) > max_bytes
    diff_text = encoded[:max_bytes].decode("utf-8", errors="ignore") if truncated else result.stdout

    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "staged": staged,
        "files": [entry["path"] for entry in files_payload],
        "file_diffs": files_payload,
        "diff": diff_text,
        "truncated": truncated,
        "total_bytes": len(encoded),
    }


def git_commit(
    *,
    cwd: Path,
    message: str,
    paths: list[str] | None = None,
    stage_all: bool = False,
    amend: bool = False,
    allow_empty: bool = False,
    author: str | None = None,
    sign_off: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, branch = repo_info
    normalized_paths = [_normalize_pathspec(path, cwd=cwd, repo_root=repo_root) for path in (paths or [])]

    if stage_all:
        if not dry_run:
            stage_result = _run_git(["add", "-A"], cwd=cwd)
            if stage_result.returncode != 0:
                return _error("git_add_failed", stage_result.stderr.strip() or "git add -A failed.", cwd=str(cwd))
    elif normalized_paths:
        if not dry_run:
            stage_result = _run_git(["add", "--", *normalized_paths], cwd=cwd)
            if stage_result.returncode != 0:
                return _error("git_add_failed", stage_result.stderr.strip() or "git add failed.", cwd=str(cwd))

    staged_result = _run_git(["diff", "--cached", "--name-only"], cwd=cwd)
    staged_files = [line for line in staged_result.stdout.splitlines() if line]
    would_stage_files: list[str] = []
    if stage_all:
        pending = _run_git(["status", "--porcelain"], cwd=cwd)
        would_stage_files = [line[3:].split(" -> ", 1)[-1] for line in pending.stdout.splitlines() if line]
    elif normalized_paths:
        would_stage_files = normalized_paths

    effective_files = staged_files + [item for item in would_stage_files if item not in staged_files]

    if not effective_files and not allow_empty and not amend:
        return _error("nothing_to_commit", "No staged changes to commit.", cwd=str(cwd))

    commit_args = ["commit", "-m", message]
    if amend:
        commit_args.append("--amend")
    if allow_empty:
        commit_args.append("--allow-empty")
    if author:
        commit_args.extend(["--author", author])
    if sign_off:
        commit_args.append("--signoff")
    # When specific paths are staged (not stage_all), restrict the commit
    # to only those paths so unrelated staged files are not committed.
    if normalized_paths and not stage_all and not amend:
        commit_args.extend(["--", *normalized_paths])

    if dry_run:
        return {
            "success": True,
            "cwd": str(cwd),
            "repo_root": str(repo_root),
            "branch": branch,
            "summary": message,
            "files": effective_files,
            "amended": amend,
            "allow_empty": allow_empty,
            "dry_run": True,
            "would_stage": would_stage_files,
            "commit_args": commit_args,
        }

    commit_result = _run_git(commit_args, cwd=cwd)
    if commit_result.returncode != 0:
        return _error(
            "git_commit_failed",
            commit_result.stderr.strip() or commit_result.stdout.strip() or "git commit failed.",
            cwd=str(cwd),
        )

    head_result = _run_git(["rev-parse", "HEAD"], cwd=cwd)
    commit_hash = head_result.stdout.strip()
    # Re-read the actual committed files from HEAD so callers see what was
    # truly committed (important when pathspec restricts the commit).
    changed = _run_git(
        ["show", "--name-only", "--pretty=format:", commit_hash],
        cwd=cwd,
    )
    committed_files = [line for line in changed.stdout.splitlines() if line]
    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "branch": branch,
        "commit": commit_hash,
        "short_commit": commit_hash[:7],
        "summary": message,
        "files": committed_files,
        "amended": amend,
        "allow_empty": allow_empty,
        "dry_run": False,
    }


def git_log(*, cwd: Path, limit: int = 10) -> dict[str, object]:
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, branch = repo_info

    result = _run_git(
        ["log", f"--max-count={max(limit, 1)}", "--pretty=format:%H%x1f%h%x1f%s%x1f%an%x1f%aI"],
        cwd=cwd,
    )
    if result.returncode != 0:
        return _error("git_log_failed", result.stderr.strip() or "git log failed.", cwd=str(cwd))

    entries: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        commit, short_commit, summary, author, committed_at = line.split("\x1f")
        entries.append(
            {
                "commit": commit,
                "short_commit": short_commit,
                "summary": summary,
                "author": author,
                "committed_at": committed_at,
            }
        )

    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "branch": branch,
        "entries": entries,
    }


def git_show(
    *,
    cwd: Path,
    ref: str = "HEAD",
    max_bytes: int = 65536,
    per_file_max_bytes: int = 16384,
) -> dict[str, object]:
    """Return metadata + diff for a single commit (or any git ref)."""
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, _branch = repo_info

    meta = _run_git(
        [
            "show",
            "--no-color",
            "--no-patch",
            "--pretty=format:%H%x1f%h%x1f%s%x1f%an%x1f%aI%x1f%P%x1f%B",
            ref,
        ],
        cwd=cwd,
    )
    if meta.returncode != 0:
        return _error(
            "git_show_failed",
            meta.stderr.strip() or f"git show failed for ref {ref!r}.",
            cwd=str(cwd),
            ref=ref,
        )
    parts = meta.stdout.split("\x1f", 6)
    if len(parts) < 7:
        return _error("git_show_failed", "Unexpected git show output.", cwd=str(cwd), ref=ref)
    commit, short_commit, summary, author, committed_at, parents_raw, body = parts
    body = body.rstrip("\n")
    parents = [p for p in parents_raw.split(" ") if p]

    diff_result = _run_git(["show", "--no-color", "--format=", ref], cwd=cwd)
    if diff_result.returncode != 0:
        return _error(
            "git_show_failed",
            diff_result.stderr.strip() or f"git show diff failed for ref {ref!r}.",
            cwd=str(cwd),
            ref=ref,
        )
    file_diffs: list[dict[str, object]] = []
    for path, chunk in _split_diff_by_file(diff_result.stdout):
        encoded_chunk = chunk.encode("utf-8")
        chunk_truncated = len(encoded_chunk) > per_file_max_bytes
        rendered = (
            encoded_chunk[:per_file_max_bytes].decode("utf-8", errors="ignore")
            if chunk_truncated
            else chunk
        )
        file_diffs.append(
            {
                "path": path,
                "diff": rendered,
                "truncated": chunk_truncated,
                "bytes": len(encoded_chunk),
            }
        )

    encoded = diff_result.stdout.encode("utf-8")
    truncated = len(encoded) > max_bytes
    diff_text = encoded[:max_bytes].decode("utf-8", errors="ignore") if truncated else diff_result.stdout

    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "ref": ref,
        "commit": commit,
        "short_commit": short_commit,
        "summary": summary,
        "author": author,
        "committed_at": committed_at,
        "parents": parents,
        "body": body,
        "files": [entry["path"] for entry in file_diffs],
        "file_diffs": file_diffs,
        "diff": diff_text,
        "truncated": truncated,
        "total_bytes": len(encoded),
    }


def git_blame(
    *,
    cwd: Path,
    path: str,
    ref: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, object]:
    """Return per-line blame info for a file."""
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, _branch = repo_info

    normalized = _normalize_pathspec(path, cwd=cwd, repo_root=repo_root)
    args = ["blame", "--porcelain"]
    if start_line is not None or end_line is not None:
        a = max(start_line or 1, 1)
        b = end_line if end_line is not None else a
        args.extend(["-L", f"{a},{b}"])
    if ref:
        args.append(ref)
    args.extend(["--", normalized])

    result = _run_git(args, cwd=cwd)
    if result.returncode != 0:
        return _error(
            "git_blame_failed",
            result.stderr.strip() or "git blame failed.",
            cwd=str(cwd),
            path=path,
            ref=ref,
        )

    # Minimal porcelain parser: each line entry starts with `<sha> <orig_line>
    # <final_line> [<group_size>]`, followed by zero or more header lines
    # (author, summary, ...) and exactly one content line prefixed with a tab.
    entries: list[dict[str, object]] = []
    meta: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in result.stdout.splitlines():
        if current is None:
            header = raw.split(" ")
            if len(header) < 3:
                continue
            sha = header[0]
            final_line = int(header[2])
            current = meta.setdefault(sha, {})
            current["__sha__"] = sha
            current["__final_line__"] = str(final_line)
            continue
        if raw.startswith("\t"):
            sha = current.get("__sha__", "")
            final_line = int(current.get("__final_line__", "0"))
            info = meta.get(sha, {})
            entries.append(
                {
                    "line": final_line,
                    "commit": sha,
                    "short_commit": sha[:7],
                    "author": info.get("author", ""),
                    "author_time": info.get("author-time", ""),
                    "summary": info.get("summary", ""),
                    "content": raw[1:],
                }
            )
            current = None
            continue
        key, _, value = raw.partition(" ")
        if key in {"author", "author-time", "summary", "filename", "previous"}:
            current[key] = value

    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "path": normalized,
        "ref": ref,
        "entries": entries,
    }
