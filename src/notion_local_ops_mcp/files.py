from __future__ import annotations

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


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if b"\x00" in raw[:1024]:
        raise ValueError("Binary files are not supported.")
    return raw.decode("utf-8", errors="replace")


def list_files(path: Path, *, recursive: bool, limit: int, offset: int = 0) -> dict[str, object]:
    if not path.exists():
        return _error("path_not_found", f"Path not found: {path}", resolved_path=str(path))
    if not path.is_dir():
        return _error("not_a_directory", f"Path is not a directory: {path}", resolved_path=str(path))

    iterator = path.rglob("*") if recursive else path.iterdir()
    entries: list[dict[str, object]] = []
    truncated = False
    entries_all = sorted(iterator, key=lambda item: str(item))
    start = max(offset, 0)
    selected = entries_all[start:]
    for index, entry in enumerate(selected):
        if limit != 0 and index >= limit:
            truncated = True
            break
        entries.append(
            {
                "name": entry.name,
                "path": str(entry),
                "is_dir": entry.is_dir(),
            }
        )
    return {
        "success": True,
        "base_path": str(path),
        "entries": entries,
        "truncated": truncated,
        "next_offset": start + len(entries) if truncated else None,
    }


def read_file(
    path: Path,
    *,
    offset: int | None,
    limit: int | None,
    max_lines: int,
    max_bytes: int,
) -> dict[str, object]:
    if not path.exists():
        return _error("file_not_found", f"File not found: {path}", resolved_path=str(path))
    if not path.is_file():
        return _error("not_a_file", f"Path is not a file: {path}", resolved_path=str(path))

    try:
        text = _read_text(path)
    except ValueError as exc:
        return _error("not_text_file", str(exc), resolved_path=str(path))

    lines = text.splitlines()
    start = max(offset or 1, 1)
    line_limit = max(limit or max_lines, 1)
    selected = lines[start - 1 : start - 1 + line_limit]
    content = "\n".join(selected)
    truncated = start - 1 + line_limit < len(lines)

    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        content = encoded[:max_bytes].decode("utf-8", errors="ignore")
        truncated = True

    return {
        "success": True,
        "path": str(path),
        "content": content,
        "truncated": truncated,
        "next_offset": start + len(selected) if truncated and selected else None,
    }


def read_files(
    paths: list[Path],
    *,
    offset: int | None,
    limit: int | None,
    max_lines: int,
    max_bytes: int,
) -> dict[str, object]:
    results = [
        read_file(path, offset=offset, limit=limit, max_lines=max_lines, max_bytes=max_bytes)
        for path in paths
    ]
    return {
        "success": all(result.get("success") is True for result in results),
        "results": results,
    }


def write_file(path: Path, *, content: str) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    return {
        "success": True,
        "path": str(path),
        "bytes_written": len(content.encode("utf-8")),
    }


def replace_in_file(
    path: Path,
    *,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> dict[str, object]:
    if not path.exists():
        return _error("file_not_found", f"File not found: {path}", resolved_path=str(path))
    if not path.is_file():
        return _error("not_a_file", f"Path is not a file: {path}", resolved_path=str(path))

    try:
        original = _read_text(path)
    except ValueError as exc:
        return _error("not_text_file", str(exc), resolved_path=str(path))

    occurrences = original.count(old_text)
    if occurrences == 0:
        return _error("match_not_found", "old_text was not found.", resolved_path=str(path))
    if occurrences > 1 and not replace_all:
        return _error(
            "match_not_unique",
            f"old_text matched {occurrences} times; provide a unique fragment.",
            resolved_path=str(path),
            occurrences=occurrences,
        )

    replacements = occurrences if replace_all else 1
    path.write_bytes(original.replace(old_text, new_text, replacements).encode("utf-8"))
    return {
        "success": True,
        "path": str(path),
        "replacements": replacements,
    }
