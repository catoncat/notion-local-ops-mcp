from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path

from .pathing import resolve_path


class PatchError(RuntimeError):
    def __init__(self, code: str, message: str, **extra: object) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


@dataclass(frozen=True)
class DiffLine:
    kind: str
    text: str


@dataclass(frozen=True)
class AddFilePatch:
    path: str
    lines: list[str]


@dataclass(frozen=True)
class DeleteFilePatch:
    path: str


@dataclass(frozen=True)
class UpdateHunk:
    lines: list[DiffLine]
    patch_line: int


@dataclass(frozen=True)
class UpdateFilePatch:
    path: str
    move_to: str | None
    hunks: list[UpdateHunk]


@dataclass(frozen=True)
class PlannedChange:
    kind: str
    path: Path
    previous_path: Path | None
    old_text: str
    new_text: str
    hunks_applied: int


PatchOperation = AddFilePatch | DeleteFilePatch | UpdateFilePatch


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


def _split_lines(text: str) -> list[str]:
    return text.splitlines()


def _join_lines(lines: list[str], *, trailing_newline: bool) -> str:
    if not lines:
        return ""
    suffix = "\n" if trailing_newline else ""
    return "\n".join(lines) + suffix


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if b"\x00" in raw[:1024]:
        raise PatchError("not_text_file", f"Binary files are not supported: {path}", path=str(path))
    return raw.decode("utf-8", errors="replace")


def _next_is_operation_header(line: str) -> bool:
    return (
        line.startswith("*** Add File: ")
        or line.startswith("*** Delete File: ")
        or line.startswith("*** Update File: ")
        or line == "*** End Patch"
    )


def _parse_add_file(lines: list[str], start: int) -> tuple[AddFilePatch, int]:
    path = lines[start][len("*** Add File: ") :]
    index = start + 1
    content: list[str] = []
    while index < len(lines) and not _next_is_operation_header(lines[index]):
        line = lines[index]
        if not line.startswith("+"):
            raise PatchError("invalid_patch", f"Add file lines must start with '+': {line}")
        content.append(line[1:])
        index += 1
    return AddFilePatch(path=path, lines=content), index


def _parse_hunk(lines: list[str], start: int) -> tuple[UpdateHunk, int]:
    patch_line = start + 1
    index = start
    if lines[index].startswith("@@"):
        index += 1

    diff_lines: list[DiffLine] = []
    while index < len(lines):
        line = lines[index]
        if _next_is_operation_header(line) or line.startswith("@@"):
            break
        if line == "*** End of File":
            index += 1
            continue
        if not line or line[0] not in {" ", "+", "-"}:
            raise PatchError("invalid_patch", f"Unexpected patch line: {line}")
        diff_lines.append(DiffLine(kind=line[0], text=line[1:]))
        index += 1

    if not diff_lines:
        raise PatchError("invalid_patch", "Update hunks must contain at least one diff line.")

    has_additions = any(line.kind == "+" for line in diff_lines)
    has_removals = any(line.kind == "-" for line in diff_lines)
    has_context = any(line.kind == " " for line in diff_lines)

    if not has_additions and not has_removals:
        raise PatchError(
            "empty_hunk",
            (
                f"Hunk at patch line {patch_line} contains only context lines; "
                "did you mean to add '-' or '+' markers?"
            ),
            patch_line=patch_line,
        )

    if has_additions and not has_removals and not has_context:
        raise PatchError(
            "unanchored_hunk",
            (
                f"Hunk at patch line {patch_line} contains only '+' lines and cannot be "
                "anchored uniquely; add surrounding context or '-' lines."
            ),
            patch_line=patch_line,
        )

    return UpdateHunk(lines=diff_lines, patch_line=patch_line), index


def _parse_update_file(lines: list[str], start: int) -> tuple[UpdateFilePatch, int]:
    path = lines[start][len("*** Update File: ") :]
    index = start + 1
    move_to: str | None = None
    if index < len(lines) and lines[index].startswith("*** Move to: "):
        move_to = lines[index][len("*** Move to: ") :]
        index += 1

    hunks: list[UpdateHunk] = []
    while index < len(lines) and not _next_is_operation_header(lines[index]):
        hunk, index = _parse_hunk(lines, index)
        hunks.append(hunk)

    if not hunks and move_to is None:
        raise PatchError("invalid_patch", f"Update file patch has no changes: {path}")
    return UpdateFilePatch(path=path, move_to=move_to, hunks=hunks), index


def parse_patch(patch: str) -> list[PatchOperation]:
    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch":
        raise PatchError("invalid_patch", "Patch must start with '*** Begin Patch'.")

    operations: list[PatchOperation] = []
    index = 1
    while index < len(lines):
        line = lines[index]
        if line == "*** End Patch":
            return operations
        if line.startswith("*** Add File: "):
            operation, index = _parse_add_file(lines, index)
            operations.append(operation)
            continue
        if line.startswith("*** Delete File: "):
            operations.append(DeleteFilePatch(path=line[len("*** Delete File: ") :]))
            index += 1
            continue
        if line.startswith("*** Update File: "):
            operation, index = _parse_update_file(lines, index)
            operations.append(operation)
            continue
        raise PatchError("invalid_patch", f"Unexpected patch header: {line}")

    raise PatchError("invalid_patch", "Patch must end with '*** End Patch'.")


def _find_sequence(lines: list[str], needle: list[str], start: int) -> int:
    if not needle:
        return -1
    max_start = len(lines) - len(needle) + 1
    for index in range(max(start, 0), max_start + 1):
        if lines[index : index + len(needle)] == needle:
            return index
    return -1


def _find_sequence_matches(lines: list[str], needle: list[str]) -> list[int]:
    if not needle:
        return []
    max_start = len(lines) - len(needle) + 1
    if max_start < 0:
        return []
    matches: list[int] = []
    for index in range(0, max_start + 1):
        if lines[index : index + len(needle)] == needle:
            matches.append(index)
    return matches


def _fuzzy_hunk_candidates(
    lines: list[str], needle: list[str], *, k: int = 3
) -> list[dict[str, object]]:
    """Return the top ``k`` line windows in ``lines`` that most resemble
    ``needle``. Each result carries a 1-based ``line``, similarity ratio and
    a short ``snippet`` so failure payloads can guide the caller.
    """
    window_size = max(len(needle), 1)
    needle_blob = "\n".join(needle)
    if not lines or not needle_blob:
        return []
    scored: list[tuple[float, int, str]] = []
    for i in range(0, max(len(lines) - window_size + 1, 1)):
        window = "\n".join(lines[i : i + window_size])
        ratio = difflib.SequenceMatcher(None, window, needle_blob, autojunk=False).ratio()
        if ratio <= 0.0:
            continue
        scored.append((ratio, i + 1, window))
    scored.sort(key=lambda item: item[0], reverse=True)
    suggestions: list[dict[str, object]] = []
    for ratio, line_no, snippet in scored[:k]:
        preview = snippet if len(snippet) <= 400 else snippet[:400] + "\u2026"
        suggestions.append(
            {
                "line": line_no,
                "similarity": round(ratio, 3),
                "snippet": preview,
            }
        )
    return suggestions


def _exact_hunk_candidates(
    lines: list[str], needle: list[str], matches: list[int], *, k: int = 3
) -> list[dict[str, object]]:
    suggestions: list[dict[str, object]] = []
    window_size = max(len(needle), 1)
    for index in matches[:k]:
        snippet = "\n".join(lines[index : index + window_size])
        preview = snippet if len(snippet) <= 400 else snippet[:400] + "\u2026"
        suggestions.append({"line": index + 1, "snippet": preview})
    return suggestions


def _apply_hunk(
    lines: list[str],
    hunk: UpdateHunk,
    cursor: int,
    *,
    path: Path,
    hunk_index: int,
) -> tuple[list[str], int]:
    old_lines = [line.text for line in hunk.lines if line.kind != "+"]
    new_lines = [line.text for line in hunk.lines if line.kind != "-"]
    search_start = max(cursor - len(old_lines), 0)
    matches = _find_sequence_matches(lines, old_lines)
    if not matches:
        raise PatchError(
            "patch_context_not_found",
            (
                f"Could not match update hunk #{hunk_index + 1} in {path}. "
                "See `candidates` for the closest line windows; the hunk's expected "
                "context is in `expected`."
            ),
            path=str(path),
            hunk_index=hunk_index,
            patch_line=hunk.patch_line,
            expected=old_lines,
            search_started_at_line=search_start + 1,
            candidates=_fuzzy_hunk_candidates(lines, old_lines, k=3),
        )
    if len(matches) != 1:
        raise PatchError(
            "ambiguous_context_match",
            (
                f"Update hunk #{hunk_index + 1} in {path} matched {len(matches)} locations. "
                "Patch context must match exactly one location; add more surrounding context."
            ),
            path=str(path),
            hunk_index=hunk_index,
            patch_line=hunk.patch_line,
            expected=old_lines,
            match_count=len(matches),
            expected_match_count=1,
            matching_lines=[match + 1 for match in matches],
            candidates=_exact_hunk_candidates(lines, old_lines, matches, k=3),
        )
    match_index = matches[0]
    updated = lines[:match_index] + new_lines + lines[match_index + len(old_lines) :]
    return updated, match_index + len(new_lines)


def _plan_update(path: Path, move_to: Path | None, hunks: list[UpdateHunk]) -> PlannedChange:
    if not path.exists():
        raise PatchError("file_not_found", f"File not found: {path}", path=str(path))
    if not path.is_file():
        raise PatchError("not_a_file", f"Path is not a file: {path}", path=str(path))

    original = _read_text(path)
    lines = _split_lines(original)
    cursor = 0
    for hunk_index, hunk in enumerate(hunks):
        lines, cursor = _apply_hunk(lines, hunk, cursor, path=path, hunk_index=hunk_index)

    target = move_to or path
    if move_to and move_to.exists() and move_to != path:
        raise PatchError("target_exists", f"Move target already exists: {move_to}", path=str(move_to))

    return PlannedChange(
        kind="move" if move_to and move_to != path else "update",
        path=target,
        previous_path=path if move_to and move_to != path else None,
        old_text=original,
        new_text=_join_lines(lines, trailing_newline=original.endswith("\n")),
        hunks_applied=len(hunks),
    )


def _plan_add(path: Path, lines: list[str]) -> PlannedChange:
    if path.exists():
        raise PatchError("path_exists", f"Path already exists: {path}", path=str(path))
    return PlannedChange(
        kind="add",
        path=path,
        previous_path=None,
        old_text="",
        new_text=_join_lines(lines, trailing_newline=bool(lines)),
        hunks_applied=1,
    )


def _plan_delete(path: Path) -> PlannedChange:
    if not path.exists():
        raise PatchError("path_not_found", f"Path not found: {path}", path=str(path))
    if path.is_dir():
        raise PatchError("not_a_file", f"Delete file patch only supports files: {path}", path=str(path))
    return PlannedChange(
        kind="delete",
        path=path,
        previous_path=None,
        old_text=_read_text(path),
        new_text="",
        hunks_applied=1,
    )


def _serialize_change(change: PlannedChange) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": change.kind,
        "path": str(change.path),
    }
    if change.previous_path is not None:
        payload["previous_path"] = str(change.previous_path)
    return payload


def _render_diff(change: PlannedChange) -> str:
    old_path = str(change.previous_path or change.path)
    new_path = str(change.path)
    return "".join(
        difflib.unified_diff(
            change.old_text.splitlines(keepends=True),
            change.new_text.splitlines(keepends=True),
            fromfile=old_path,
            tofile=new_path,
        )
    )


def _apply_change(change: PlannedChange) -> None:
    if change.kind == "delete":
        change.path.unlink()
        return
    change.path.parent.mkdir(parents=True, exist_ok=True)
    change.path.write_text(change.new_text, encoding="utf-8")
    if change.kind == "move" and change.previous_path is not None and change.previous_path != change.path:
        change.previous_path.unlink()


def _diff_line_counts(diff_text: str) -> tuple[int, int]:
    lines_added = 0
    lines_removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            lines_added += 1
            continue
        if line.startswith("-"):
            lines_removed += 1
    return lines_added, lines_removed


def _change_warnings(change: PlannedChange, *, lines_added: int, lines_removed: int) -> list[str]:
    warnings: list[str] = []
    if change.kind in {"update", "move"} and lines_added > 0 and lines_removed == 0:
        warnings.append(
            "update inserted lines without removing any existing lines; verify this was intended"
        )
    return warnings


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _summarize_change(change: PlannedChange, *, diff_text: str) -> dict[str, object]:
    lines_added, lines_removed = _diff_line_counts(diff_text)
    warnings = _change_warnings(change, lines_added=lines_added, lines_removed=lines_removed)
    payload: dict[str, object] = {
        "kind": change.kind,
        "path": str(change.path),
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "bytes_before": len(change.old_text.encode("utf-8")),
        "bytes_after": len(change.new_text.encode("utf-8")),
        "hunks_applied": change.hunks_applied,
        "sha256_after": _sha256_text(change.new_text),
        "warnings": warnings,
    }
    if change.previous_path is not None:
        payload["previous_path"] = str(change.previous_path)
    return payload


def apply_patch(
    *,
    patch: str,
    workspace_root: Path,
    dry_run: bool = False,
    validate_only: bool = False,
    return_diff: bool = False,
) -> dict[str, object]:
    try:
        operations = parse_patch(patch)
        planned_changes: list[PlannedChange] = []
        for operation in operations:
            if isinstance(operation, AddFilePatch):
                planned_changes.append(_plan_add(resolve_path(operation.path, workspace_root), operation.lines))
                continue
            if isinstance(operation, DeleteFilePatch):
                planned_changes.append(_plan_delete(resolve_path(operation.path, workspace_root)))
                continue
            target = resolve_path(operation.path, workspace_root)
            move_to = resolve_path(operation.move_to, workspace_root) if operation.move_to else None
            planned_changes.append(_plan_update(target, move_to, operation.hunks))

        rendered_diffs = [_render_diff(change) for change in planned_changes]
        file_summaries = [
            _summarize_change(change, diff_text=diff_text)
            for change, diff_text in zip(planned_changes, rendered_diffs, strict=True)
        ]
        warnings = list(
            dict.fromkeys(
                warning
                for file_summary in file_summaries
                for warning in file_summary.get("warnings", [])
            )
        )

        should_apply = not dry_run and not validate_only
        if should_apply:
            for change in planned_changes:
                _apply_change(change)

        payload: dict[str, object] = {
            "success": True,
            "changes": [_serialize_change(change) for change in planned_changes],
            "files": file_summaries,
            "warnings": warnings,
            "applied": should_apply,
            "validated": dry_run or validate_only,
        }
        if return_diff:
            payload["diff"] = "".join(rendered_diffs)
        return payload
    except PatchError as exc:
        return _error(exc.code, str(exc), **exc.extra)
