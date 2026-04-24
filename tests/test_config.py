from __future__ import annotations

import math

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


# ---------------------------------------------------------------------------
# Config validation tests (reject nan/inf, illegal port/timeout)
# ---------------------------------------------------------------------------


def test_stream_output_interval_rejects_nan(monkeypatch) -> None:
    monkeypatch.setenv("NOTION_LOCAL_OPS_STREAM_OUTPUT_INTERVAL", "nan")
    with pytest.raises(ValueError, match="finite"):
        config._env_float("NOTION_LOCAL_OPS_STREAM_OUTPUT_INTERVAL", 0.5, min_value=0.01)


def test_stream_output_interval_rejects_inf(monkeypatch) -> None:
    monkeypatch.setenv("NOTION_LOCAL_OPS_STREAM_OUTPUT_INTERVAL", "inf")
    with pytest.raises(ValueError, match="finite"):
        config._env_float("NOTION_LOCAL_OPS_STREAM_OUTPUT_INTERVAL", 0.5, min_value=0.01)


def test_port_rejects_zero(monkeypatch) -> None:
    monkeypatch.setenv("NOTION_LOCAL_OPS_PORT", "0")
    with pytest.raises(ValueError, match=">= 1"):
        config._env_int("NOTION_LOCAL_OPS_PORT", 8766, min_value=1, max_value=65535)


def test_port_rejects_above_65535(monkeypatch) -> None:
    monkeypatch.setenv("NOTION_LOCAL_OPS_PORT", "70000")
    with pytest.raises(ValueError, match="<= 65535"):
        config._env_int("NOTION_LOCAL_OPS_PORT", 8766, min_value=1, max_value=65535)


def test_command_timeout_rejects_zero(monkeypatch) -> None:
    monkeypatch.setenv("NOTION_LOCAL_OPS_COMMAND_TIMEOUT", "0")
    with pytest.raises(ValueError, match=">= 1"):
        config._env_int("NOTION_LOCAL_OPS_COMMAND_TIMEOUT", 120, min_value=1)


def test_delegate_timeout_rejects_zero(monkeypatch) -> None:
    monkeypatch.setenv("NOTION_LOCAL_OPS_DELEGATE_TIMEOUT", "0")
    with pytest.raises(ValueError, match=">= 1"):
        config._env_int("NOTION_LOCAL_OPS_DELEGATE_TIMEOUT", 1800, min_value=1)


def test_http_keepalive_timeout_rejects_zero(monkeypatch) -> None:
    monkeypatch.setenv("NOTION_LOCAL_OPS_HTTP_KEEPALIVE_TIMEOUT", "0")
    with pytest.raises(ValueError, match=">= 1"):
        config._env_optional_int("NOTION_LOCAL_OPS_HTTP_KEEPALIVE_TIMEOUT", min_value=1)


def test_http_keepalive_timeout_unset_returns_none(monkeypatch) -> None:
    monkeypatch.delenv("NOTION_LOCAL_OPS_HTTP_KEEPALIVE_TIMEOUT", raising=False)
    result = config._env_optional_int("NOTION_LOCAL_OPS_HTTP_KEEPALIVE_TIMEOUT", min_value=1)
    assert result is None


def test_stream_output_interval_rejects_zero(monkeypatch) -> None:
    monkeypatch.setenv("NOTION_LOCAL_OPS_STREAM_OUTPUT_INTERVAL", "0")
    with pytest.raises(ValueError, match=">= 0.01"):
        config._env_float("NOTION_LOCAL_OPS_STREAM_OUTPUT_INTERVAL", 0.5, min_value=0.01)


def test_stream_output_interval_rejects_negative(monkeypatch) -> None:
    monkeypatch.setenv("NOTION_LOCAL_OPS_STREAM_OUTPUT_INTERVAL", "-1")
    with pytest.raises(ValueError, match=">= 0.01"):
        config._env_float("NOTION_LOCAL_OPS_STREAM_OUTPUT_INTERVAL", 0.5, min_value=0.01)