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

import os
from pathlib import Path


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


APP_NAME = "notion-local-ops-mcp"
HOST = os.environ.get("NOTION_LOCAL_OPS_HOST", "127.0.0.1")
PORT = int(os.environ.get("NOTION_LOCAL_OPS_PORT", "8766"))

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
AUTH_MODE = os.environ.get("NOTION_LOCAL_OPS_AUTH_MODE", "").strip().lower()
PUBLIC_BASE_URL = os.environ.get("NOTION_LOCAL_OPS_PUBLIC_BASE_URL", "").strip().rstrip("/")
OAUTH_LOGIN_TOKEN = os.environ.get("NOTION_LOCAL_OPS_OAUTH_LOGIN_TOKEN", "").strip()
OAUTH_SCOPES = tuple(
    scope
    for scope in os.environ.get("NOTION_LOCAL_OPS_OAUTH_SCOPES", "local-ops").split()
    if scope
)
OAUTH_TOKEN_TTL_SECONDS = int(os.environ.get("NOTION_LOCAL_OPS_OAUTH_TOKEN_TTL_SECONDS", "86400"))
CODEX_COMMAND = os.environ.get("NOTION_LOCAL_OPS_CODEX_COMMAND", "codex").strip()
CLAUDE_COMMAND = os.environ.get("NOTION_LOCAL_OPS_CLAUDE_COMMAND", "claude").strip()
COMMAND_TIMEOUT = int(os.environ.get("NOTION_LOCAL_OPS_COMMAND_TIMEOUT", "120"))
DELEGATE_TIMEOUT = int(os.environ.get("NOTION_LOCAL_OPS_DELEGATE_TIMEOUT", "1800"))
DEBUG_MCP_LOGGING = _env_flag("NOTION_LOCAL_OPS_DEBUG_MCP_LOGGING", default=False)
GRACEFUL_SHUTDOWN_SECONDS = int(
    os.environ.get("NOTION_LOCAL_OPS_GRACEFUL_SHUTDOWN_SECONDS", "30")
)


def ensure_runtime_directories() -> None:
    if not WORKSPACE_ROOT.exists():
        raise FileNotFoundError(f"Default cwd does not exist: {WORKSPACE_ROOT}")
    if not WORKSPACE_ROOT.is_dir():
        raise NotADirectoryError(f"Default cwd is not a directory: {WORKSPACE_ROOT}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
