from __future__ import annotations

import pytest

from notion_local_ops_mcp import config


def test_ensure_runtime_directories_requires_existing_workspace_root(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root = tmp_path / "missing-workspace"
    state_dir = tmp_path / "state"

    monkeypatch.setattr(config, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)

    with pytest.raises(FileNotFoundError):
        config.ensure_runtime_directories()

    assert workspace_root.exists() is False
    assert state_dir.exists() is False


def test_ensure_runtime_directories_creates_state_dir_for_valid_workspace(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    workspace_root.mkdir()

    monkeypatch.setattr(config, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)

    config.ensure_runtime_directories()

    assert workspace_root.is_dir() is True
    assert state_dir.is_dir() is True
