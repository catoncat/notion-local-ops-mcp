"""Process-wide configuration loaded from environment variables.

Semantic note on ``WORKSPACE_ROOT`` / ``DEFAULT_CWD``
-----------------------------------------------------
Despite the name "root", this value is **not a sandbox boundary**. The project
is designed to give an MCP client (Notion Agent / Codex / Claude) arbitrary
local-shell capability; once a client passes the bearer token it has full shell
and full-filesystem access.

``WORKSPACE_ROOT`` is only used for two things:

1. **Relative-path anchor.** :func:`notion_local_ops_mcp.pathing.resolve_path`
   joins relative inputs onto it; absolute paths are returned as-is.
2. **Default ``cwd``.** :func:`notion_local_ops_mcp.pathing.resolve_cwd`
   falls back to it when neither the tool call nor the session-level override
   (``set_default_cwd``) provides a directory.

It therefore behaves like a *default working directory*, not a root. The
``DEFAULT_CWD`` alias below reflects that; ``WORKSPACE_ROOT`` is kept for
back-compat. The environment variable name ``NOTION_LOCAL_OPS_WORKSPACE_ROOT``
stays unchanged to avoid breaking existing setups.
"""

from __future__ import annotations

import math
import os
from pathlib import Path


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(
    name: str,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    raw = os.environ.get(name)
    value = int(raw) if raw is not None else default
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}")
    if max_value is not None and value > max_value:
        raise ValueError(f"{name} must be <= {max_value}, got {value}")
    return value


def _env_float(
    name: str,
    default: float,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    raw = os.environ.get(name)
    value = float(raw) if raw is not None else default
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value}")
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}")
    if max_value is not None and value > max_value:
        raise ValueError(f"{name} must be <= {max_value}, got {value}")
    return value


def _env_optional_int(
    name: str,
    *,
    min_value: int | None = None,
) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = int(raw)
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value} when set, got {value}")
    return value


APP_NAME = "notion-local-ops-mcp"
HOST = os.environ.get("NOTION_LOCAL_OPS_HOST", "127.0.0.1")
PORT = _env_int("NOTION_LOCAL_OPS_PORT", 8766, min_value=1, max_value=65535)

# Default cwd for tool calls (see module docstring). Kept as WORKSPACE_ROOT
# for back-compat; DEFAULT_CWD is the preferred name going forward.
WORKSPACE_ROOT = Path(
    os.environ.get("NOTION_LOCAL_OPS_WORKSPACE_ROOT", str(Path.home()))
).expanduser().resolve()
DEFAULT_CWD = WORKSPACE_ROOT

STATE_DIR = Path(
    os.environ.get("NOTION_LOCAL_OPS_STATE_DIR", str(Path.home() / ".notion-local-ops-mcp"))
).expanduser().resolve()
AUTH_TOKEN = os.environ.get("NOTION_LOCAL_OPS_AUTH_TOKEN", "").strip()
CODEX_COMMAND = os.environ.get("NOTION_LOCAL_OPS_CODEX_COMMAND", "codex").strip()
CLAUDE_COMMAND = os.environ.get("NOTION_LOCAL_OPS_CLAUDE_COMMAND", "claude").strip()
COMMAND_TIMEOUT = _env_int("NOTION_LOCAL_OPS_COMMAND_TIMEOUT", 120, min_value=1)
DELEGATE_TIMEOUT = _env_int("NOTION_LOCAL_OPS_DELEGATE_TIMEOUT", 1800, min_value=1)
DEBUG_MCP_LOGGING = _env_flag("NOTION_LOCAL_OPS_DEBUG_MCP_LOGGING", default=False)

# HTTP keep-alive timeout for uvicorn (None = unlimited/infinite)
# Set to a positive integer (e.g., 3600) to enforce a max idle time
HTTP_KEEPALIVE_TIMEOUT = _env_optional_int("NOTION_LOCAL_OPS_HTTP_KEEPALIVE_TIMEOUT", min_value=1)

# Stream output interval in seconds for long-running tasks (default 0.5s)
STREAM_OUTPUT_INTERVAL = _env_float(
    "NOTION_LOCAL_OPS_STREAM_OUTPUT_INTERVAL",
    0.5,
    min_value=0.01,
)


def ensure_runtime_directories() -> None:
    if not WORKSPACE_ROOT.exists():
        raise FileNotFoundError(f"Default cwd does not exist: {WORKSPACE_ROOT}")
    if not WORKSPACE_ROOT.is_dir():
        raise NotADirectoryError(f"Default cwd is not a directory: {WORKSPACE_ROOT}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)