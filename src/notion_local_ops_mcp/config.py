from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "notion-local-ops-mcp"
HOST = os.environ.get("NOTION_LOCAL_OPS_HOST", "127.0.0.1")
PORT = int(os.environ.get("NOTION_LOCAL_OPS_PORT", "8766"))
WORKSPACE_ROOT = Path(
    os.environ.get("NOTION_LOCAL_OPS_WORKSPACE_ROOT", str(Path.home()))
).expanduser().resolve()
STATE_DIR = Path(
    os.environ.get("NOTION_LOCAL_OPS_STATE_DIR", str(Path.home() / ".notion-local-ops-mcp"))
).expanduser().resolve()
AUTH_TOKEN = os.environ.get("NOTION_LOCAL_OPS_AUTH_TOKEN", "").strip()
CODEX_COMMAND = os.environ.get("NOTION_LOCAL_OPS_CODEX_COMMAND", "codex").strip()
CLAUDE_COMMAND = os.environ.get("NOTION_LOCAL_OPS_CLAUDE_COMMAND", "claude").strip()
COMMAND_TIMEOUT = int(os.environ.get("NOTION_LOCAL_OPS_COMMAND_TIMEOUT", "30"))
DELEGATE_TIMEOUT = int(os.environ.get("NOTION_LOCAL_OPS_DELEGATE_TIMEOUT", "1800"))


def ensure_runtime_directories() -> None:
    if not WORKSPACE_ROOT.exists():
        raise FileNotFoundError(f"Workspace root does not exist: {WORKSPACE_ROOT}")
    if not WORKSPACE_ROOT.is_dir():
        raise NotADirectoryError(f"Workspace root is not a directory: {WORKSPACE_ROOT}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
