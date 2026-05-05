#!/usr/bin/env bash
set -euo pipefail

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/launchd-common.sh"

prepare_launchd_env
require_command launchctl
require_command cloudflared

PYTHON_BIN="$(pick_python)"
if [[ ! -d "${ROOT_DIR}/.venv" ]]; then
  "${PYTHON_BIN}" -m venv "${ROOT_DIR}/.venv"
fi
# shellcheck disable=SC1091
source "${ROOT_DIR}/.venv/bin/activate"

ensure_python_runtime_deps

if [[ -z "${NOTION_LOCAL_OPS_AUTH_TOKEN:-}" ]]; then
  echo "Missing NOTION_LOCAL_OPS_AUTH_TOKEN. Set it in .env or export it before installing launchd services." >&2
  exit 1
fi

if ! CLOUDFLARED_CONFIG="$(pick_cloudflared_config)"; then
  echo "A named cloudflared config is required for launchd install. Set NOTION_LOCAL_OPS_CLOUDFLARED_CONFIG or add cloudflared.local.yml." >&2
  exit 1
fi

CLOUDFLARED_BIN="$(command -v cloudflared)"
MCP_LABEL="$(mcp_label)"
CLOUDFLARED_LABEL="$(cloudflared_label)"
MCP_TARGET="$(launchctl_target "${MCP_LABEL}")"
CLOUDFLARED_TARGET="$(launchctl_target "${CLOUDFLARED_LABEL}")"
MCP_PLIST="$(plist_path_for_label "${MCP_LABEL}")"
CLOUDFLARED_PLIST="$(plist_path_for_label "${CLOUDFLARED_LABEL}")"

launchctl bootout "${MCP_TARGET}" 2>/dev/null || true
launchctl bootout "${CLOUDFLARED_TARGET}" 2>/dev/null || true
sleep 1

if lsof -nP -iTCP:"${NOTION_LOCAL_OPS_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port ${NOTION_LOCAL_OPS_PORT} is already in use. Stop manual dev-tunnel/tmux processes before installing launchd services." >&2
  exit 1
fi

mkdir -p "${NOTION_LOCAL_OPS_LAUNCHD_DIR}" "${NOTION_LOCAL_OPS_LAUNCHD_LOG_DIR}" "${NOTION_LOCAL_OPS_STATE_DIR}"
export ROOT_DIR CLOUDFLARED_BIN CLOUDFLARED_CONFIG MCP_PLIST CLOUDFLARED_PLIST
python - <<'PY'
import os
from pathlib import Path

from notion_local_ops_mcp.launchd_support import (
    LaunchdServiceConfig,
    build_cloudflared_launch_agent,
    build_mcp_launch_agent,
    write_launch_agent,
)

config = LaunchdServiceConfig(
    repo_root=Path(os.environ["ROOT_DIR"]),
    launch_agents_dir=Path(os.environ["NOTION_LOCAL_OPS_LAUNCHD_DIR"]),
    logs_dir=Path(os.environ["NOTION_LOCAL_OPS_LAUNCHD_LOG_DIR"]),
    label_prefix=os.environ["NOTION_LOCAL_OPS_LAUNCHD_LABEL_PREFIX"],
    python_bin=Path(os.environ["ROOT_DIR"]) / ".venv" / "bin" / "python",
    cloudflared_bin=Path(os.environ["CLOUDFLARED_BIN"]),
    cloudflared_config=Path(os.environ["CLOUDFLARED_CONFIG"]),
    tunnel_name=os.environ.get("NOTION_LOCAL_OPS_TUNNEL_NAME") or None,
    env={
        key: value
        for key, value in os.environ.items()
        if key in {
            "PATH",
            "NOTION_LOCAL_OPS_HOST",
            "NOTION_LOCAL_OPS_PORT",
            "NOTION_LOCAL_OPS_WORKSPACE_ROOT",
            "NOTION_LOCAL_OPS_STATE_DIR",
            "NOTION_LOCAL_OPS_AUTH_TOKEN",
            "NOTION_LOCAL_OPS_AUTH_MODE",
            "NOTION_LOCAL_OPS_PUBLIC_BASE_URL",
            "NOTION_LOCAL_OPS_OAUTH_LOGIN_TOKEN",
            "NOTION_LOCAL_OPS_OAUTH_SCOPES",
            "NOTION_LOCAL_OPS_OAUTH_TOKEN_TTL_SECONDS",
            "NOTION_LOCAL_OPS_CODEX_COMMAND",
            "NOTION_LOCAL_OPS_CLAUDE_COMMAND",
            "NOTION_LOCAL_OPS_COMMAND_TIMEOUT",
            "NOTION_LOCAL_OPS_DELEGATE_TIMEOUT",
            "NOTION_LOCAL_OPS_DEBUG_MCP_LOGGING",
            "NOTION_LOCAL_OPS_GRACEFUL_SHUTDOWN_SECONDS",
        }
    },
)
config = LaunchdServiceConfig(
    repo_root=config.repo_root,
    launch_agents_dir=config.launch_agents_dir,
    logs_dir=config.logs_dir,
    label_prefix=config.label_prefix,
    python_bin=config.python_bin,
    cloudflared_bin=config.cloudflared_bin,
    cloudflared_config=config.cloudflared_config,
    tunnel_name=config.tunnel_name,
    env={**config.env, "PATH": os.environ["NOTION_LOCAL_OPS_LAUNCHD_PATH"]},
)
write_launch_agent(Path(os.environ["MCP_PLIST"]), build_mcp_launch_agent(config))
write_launch_agent(Path(os.environ["CLOUDFLARED_PLIST"]), build_cloudflared_launch_agent(config))
PY

launchctl bootstrap "gui/${UID}" "${MCP_PLIST}"
launchctl bootstrap "gui/${UID}" "${CLOUDFLARED_PLIST}"
sleep 2
launchctl kickstart -k "${MCP_TARGET}"
launchctl kickstart -k "${CLOUDFLARED_TARGET}"
sleep 4

if ! curl -fsSI "http://${NOTION_LOCAL_OPS_HOST}:${NOTION_LOCAL_OPS_PORT}/mcp" >/dev/null 2>&1; then
  echo "Launchd services installed, but local /mcp is not reachable yet. Check launchctl print ${MCP_TARGET} and logs under ${NOTION_LOCAL_OPS_LAUNCHD_LOG_DIR}." >&2
  exit 1
fi

echo "Installed launchd services:"
echo "- MCP:         ${MCP_TARGET}"
echo "- cloudflared: ${CLOUDFLARED_TARGET}"
echo "Plists:"
echo "- ${MCP_PLIST}"
echo "- ${CLOUDFLARED_PLIST}"
echo "Logs: ${NOTION_LOCAL_OPS_LAUNCHD_LOG_DIR}"
echo "Use ./scripts/launchd-status.sh to inspect, ./scripts/launchd-reload.sh for code reload, and ./scripts/launchd-restart.sh all for full restarts."
