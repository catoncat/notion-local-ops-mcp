from pathlib import Path

from notion_local_ops_mcp.patching import apply_patch


def test_apply_patch_returns_candidates_when_context_missing(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text(
        "def greet(name):\n    print('hello ' + name)\n\n"
        "def farewell(name):\n    print('bye ' + name)\n",
        encoding="utf-8",
    )

    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: app.py",
                "@@",
                " def greet(name):",
                "-    print('hi ' + name)",
                "+    print('HELLO ' + name)",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "patch_context_not_found"
    assert result["hunk_index"] == 0
    assert result["expected"] == ["def greet(name):", "    print('hi ' + name)"]
    candidates = result["candidates"]
    assert isinstance(candidates, list) and candidates
    top = candidates[0]
    assert {"line", "similarity", "snippet"} <= set(top)
    # File untouched on failure.
    assert "hi " not in target.read_text(encoding="utf-8")


def test_apply_patch_adds_file(tmp_path: Path) -> None:
    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Add File: notes.txt",
                "+hello",
                "+world",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
    )

    assert result["success"] is True
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "hello\nworld\n"


def test_apply_patch_updates_file_with_multiple_hunks(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: app.py",
                "@@",
                " one",
                "-two",
                "+TWO",
                " three",
                "@@",
                " three",
                "-four",
                "+FOUR",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
    )

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "one\nTWO\nthree\nFOUR\n"


def test_apply_patch_moves_and_updates_file(tmp_path: Path) -> None:
    source = tmp_path / "src.txt"
    source.write_text("alpha\nbeta\n", encoding="utf-8")

    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: src.txt",
                "*** Move to: moved.txt",
                "@@",
                " alpha",
                "-beta",
                "+gamma",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
    )

    assert result["success"] is True
    assert source.exists() is False
    assert (tmp_path / "moved.txt").read_text(encoding="utf-8") == "alpha\ngamma\n"
    assert result["changes"][0]["kind"] == "move"


def test_apply_patch_deletes_file(tmp_path: Path) -> None:
    target = tmp_path / "trash.txt"
    target.write_text("bye\n", encoding="utf-8")

    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Delete File: trash.txt",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
    )

    assert result["success"] is True
    assert target.exists() is False


def test_apply_patch_dry_run_returns_diff_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("before\n", encoding="utf-8")

    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: app.py",
                "@@",
                "-before",
                "+after",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
        dry_run=True,
        return_diff=True,
    )

    assert result["success"] is True
    assert result["applied"] is False
    assert result["diff"].startswith("--- ")
    assert target.read_text(encoding="utf-8") == "before\n"


def test_apply_patch_validate_only_checks_patch_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("hello\n", encoding="utf-8")

    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: note.txt",
                "@@",
                "-hello",
                "+world",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
        validate_only=True,
    )

    assert result["success"] is True
    assert result["validated"] is True
    assert result["applied"] is False
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_apply_patch_rejects_context_only_hunk(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: note.txt",
                "@@",
                " alpha",
                " beta",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "empty_hunk"
    assert result["patch_line"] == 3


def test_apply_patch_requires_unique_context_match(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("alpha\nbeta\nalpha\nbeta\n", encoding="utf-8")

    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: note.txt",
                "@@",
                " alpha",
                "-beta",
                "+BETA",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "ambiguous_context_match"
    assert result["match_count"] == 2
    assert result["expected_match_count"] == 1
    assert result["matching_lines"] == [1, 3]


def test_apply_patch_returns_change_stats_and_warnings(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("alpha\nomega\n", encoding="utf-8")
    bytes_before = len(target.read_bytes())

    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: note.txt",
                "@@",
                " alpha",
                "+beta",
                " omega",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
        return_diff=True,
    )

    assert result["success"] is True
    file_summary = result["files"][0]
    assert file_summary["path"] == str(target)
    assert file_summary["kind"] == "update"
    assert file_summary["lines_added"] == 1
    assert file_summary["lines_removed"] == 0
    assert file_summary["bytes_before"] == bytes_before
    assert file_summary["bytes_after"] == len(target.read_bytes())
    assert file_summary["hunks_applied"] == 1
    assert len(file_summary["sha256_after"]) == 64
    assert file_summary["warnings"] == [
        "update inserted lines without removing any existing lines; verify this was intended"
    ]
    assert result["warnings"] == file_summary["warnings"]
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\nomega\n"


def test_apply_patch_rejects_duplicate_update_same_file(tmp_path: Path) -> None:
    """Same file in two separate Update operations should be rejected."""
    target = tmp_path / "app.py"
    target.write_text("hello\n", encoding="utf-8")

    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: app.py",
                "@@",
                "-hello",
                "+HELLO",
                "*** Update File: app.py",
                "@@",
                "-HELLO",
                "+hello",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "duplicate_patch_target"


def test_apply_patch_rejects_add_then_update_same_file(tmp_path: Path) -> None:
    """Add then Update for the same file should be rejected."""
    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Add File: new.txt",
                "+content",
                "*** Update File: new.txt",
                "@@",
                "-content",
                "+CONTENT",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "duplicate_patch_target"


def test_apply_patch_allows_multiple_hunks_in_single_update_file(tmp_path: Path) -> None:
    """Multiple hunks within a single Update File operation should work fine."""
    target = tmp_path / "app.py"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: app.py",
                "@@",
                " one",
                "-two",
                "+TWO",
                " three",
                "@@",
                " three",
                "-four",
                "+FOUR",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
    )

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "one\nTWO\nthree\nFOUR\n"


def test_apply_patch_rejects_duplicate_with_canonical_path_equivalence(tmp_path: Path) -> None:
    """'app.py' and './app.py' resolve to the same canonical path — reject."""
    target = tmp_path / "app.py"
    target.write_text("hello\n", encoding="utf-8")

    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: app.py",
                "@@",
                "-hello",
                "+HELLO",
                "*** Delete File: ./app.py",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "duplicate_patch_target"


def test_apply_patch_rejects_move_to_collision(tmp_path: Path) -> None:
    """Update a.py → Move to: b.py collides with Add File: b.py — reject."""
    source = tmp_path / "a.py"
    source.write_text("hello\n", encoding="utf-8")

    result = apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                "*** Update File: a.py",
                "*** Move to: b.py",
                "@@",
                "-hello",
                "+world",
                "*** Add File: b.py",
                "+extra",
                "*** End Patch",
            ]
        ),
        workspace_root=tmp_path,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "duplicate_patch_target"
