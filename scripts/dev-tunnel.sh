#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage: ./scripts/dev-tunnel.sh

Starts the local MCP server and exposes it with cloudflared.

Environment loading order:
1. .env in the repository root
2. Current shell environment overrides matching keys

Required:
- NOTION_LOCAL_OPS_AUTH_TOKEN

Optional:
- NOTION_LOCAL_OPS_WORKSPACE_ROOT (defaults to repo root)
- NOTION_LOCAL_OPS_HOST (defaults to 127.0.0.1)
- NOTION_LOCAL_OPS_PORT (defaults to 8766)
- NOTION_LOCAL_OPS_CLOUDFLARED_CONFIG (named tunnel config path)
- NOTION_LOCAL_OPS_TUNNEL_NAME (optional override for cloudflared tunnel run)

If ./cloudflared.local.yml or ./cloudflared.local.yaml exists, this script
uses that named tunnel config automatically. Otherwise it falls back to a
cloudflared quick tunnel.
EOF
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

pick_python() {
  local candidate
  for candidate in "${PYTHON_BIN:-}" python3.11 python3; do
    if [[ -n "${candidate}" ]] && command -v "${candidate}" >/dev/null 2>&1; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  echo "Python 3.11+ is required but no suitable interpreter was found." >&2
  exit 1
}

load_env_file() {
  if [[ -f "${ROOT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${ROOT_DIR}/.env"
    set +a
  fi
}

resolve_path() {
  local value="$1"
  if [[ "${value}" = /* ]]; then
    printf '%s\n' "${value}"
    return 0
  fi
  printf '%s\n' "${ROOT_DIR}/${value}"
}

pick_cloudflared_config() {
  local candidate

  if [[ -n "${NOTION_LOCAL_OPS_CLOUDFLARED_CONFIG:-}" ]]; then
    resolve_path "${NOTION_LOCAL_OPS_CLOUDFLARED_CONFIG}"
    return 0
  fi

  for candidate in \
    "${ROOT_DIR}/cloudflared.local.yml" \
    "${ROOT_DIR}/cloudflared.local.yaml"; do
    if [[ -f "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done

  return 1
}

wait_for_server() {
  python - <<'PY'
import os
import socket
import sys
import time

host = os.environ["NOTION_LOCAL_OPS_HOST"]
port = int(os.environ["NOTION_LOCAL_OPS_PORT"])

deadline = time.time() + 15
while time.time() < deadline:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        if sock.connect_ex((host, port)) == 0:
            raise SystemExit(0)
    time.sleep(0.2)

print(f"Timed out waiting for {host}:{port}", file=sys.stderr)
raise SystemExit(1)
PY
}

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    kill "${SERVER_PID}" >/dev/null 2>&1 || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
  exit "${exit_code}"
}

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

trap cleanup EXIT INT TERM

require_command cloudflared

PYTHON_BIN="$(pick_python)"
if [[ ! -d "${ROOT_DIR}/.venv" ]]; then
  "${PYTHON_BIN}" -m venv "${ROOT_DIR}/.venv"
fi

# shellcheck disable=SC1091
source "${ROOT_DIR}/.venv/bin/activate"

python - <<'PY'
import sys

if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required.")
PY

if ! command -v notion-local-ops-mcp >/dev/null 2>&1 || ! python - <<'PY' >/dev/null 2>&1
import fastmcp
import uvicorn
PY
then
  python -m pip install -r requirements.txt
  python -m pip install -e .
fi

OVERRIDE_HOST="${NOTION_LOCAL_OPS_HOST:-}"
OVERRIDE_PORT="${NOTION_LOCAL_OPS_PORT:-}"
OVERRIDE_WORKSPACE_ROOT="${NOTION_LOCAL_OPS_WORKSPACE_ROOT:-}"
OVERRIDE_STATE_DIR="${NOTION_LOCAL_OPS_STATE_DIR:-}"
OVERRIDE_AUTH_TOKEN="${NOTION_LOCAL_OPS_AUTH_TOKEN:-}"
OVERRIDE_CLOUDFLARED_CONFIG="${NOTION_LOCAL_OPS_CLOUDFLARED_CONFIG:-}"
OVERRIDE_TUNNEL_NAME="${NOTION_LOCAL_OPS_TUNNEL_NAME:-}"
OVERRIDE_CODEX_COMMAND="${NOTION_LOCAL_OPS_CODEX_COMMAND:-}"
OVERRIDE_CLAUDE_COMMAND="${NOTION_LOCAL_OPS_CLAUDE_COMMAND:-}"
OVERRIDE_COMMAND_TIMEOUT="${NOTION_LOCAL_OPS_COMMAND_TIMEOUT:-}"
OVERRIDE_DELEGATE_TIMEOUT="${NOTION_LOCAL_OPS_DELEGATE_TIMEOUT:-}"

load_env_file

export NOTION_LOCAL_OPS_HOST="${OVERRIDE_HOST:-${NOTION_LOCAL_OPS_HOST:-127.0.0.1}}"
export NOTION_LOCAL_OPS_PORT="${OVERRIDE_PORT:-${NOTION_LOCAL_OPS_PORT:-8766}}"
export NOTION_LOCAL_OPS_WORKSPACE_ROOT="${OVERRIDE_WORKSPACE_ROOT:-${NOTION_LOCAL_OPS_WORKSPACE_ROOT:-${ROOT_DIR}}}"

if [[ -n "${OVERRIDE_STATE_DIR}" ]]; then
  export NOTION_LOCAL_OPS_STATE_DIR="${OVERRIDE_STATE_DIR}"
fi

if [[ -n "${OVERRIDE_AUTH_TOKEN}" ]]; then
  export NOTION_LOCAL_OPS_AUTH_TOKEN="${OVERRIDE_AUTH_TOKEN}"
fi

if [[ -n "${OVERRIDE_CLOUDFLARED_CONFIG}" ]]; then
  export NOTION_LOCAL_OPS_CLOUDFLARED_CONFIG="${OVERRIDE_CLOUDFLARED_CONFIG}"
fi

if [[ -n "${OVERRIDE_TUNNEL_NAME}" ]]; then
  export NOTION_LOCAL_OPS_TUNNEL_NAME="${OVERRIDE_TUNNEL_NAME}"
fi

if [[ -n "${OVERRIDE_CODEX_COMMAND}" ]]; then
  export NOTION_LOCAL_OPS_CODEX_COMMAND="${OVERRIDE_CODEX_COMMAND}"
fi

if [[ -n "${OVERRIDE_CLAUDE_COMMAND}" ]]; then
  export NOTION_LOCAL_OPS_CLAUDE_COMMAND="${OVERRIDE_CLAUDE_COMMAND}"
fi

if [[ -n "${OVERRIDE_COMMAND_TIMEOUT}" ]]; then
  export NOTION_LOCAL_OPS_COMMAND_TIMEOUT="${OVERRIDE_COMMAND_TIMEOUT}"
fi

if [[ -n "${OVERRIDE_DELEGATE_TIMEOUT}" ]]; then
  export NOTION_LOCAL_OPS_DELEGATE_TIMEOUT="${OVERRIDE_DELEGATE_TIMEOUT}"
fi

if [[ -z "${NOTION_LOCAL_OPS_AUTH_TOKEN:-}" ]]; then
  echo "Missing NOTION_LOCAL_OPS_AUTH_TOKEN. Set it in .env or export it before running." >&2
  exit 1
fi

SERVER_URL="http://${NOTION_LOCAL_OPS_HOST}:${NOTION_LOCAL_OPS_PORT}"
SERVER_LOG="${TMPDIR:-/tmp}/notion-local-ops-mcp-server.$$.log"

echo "Starting notion-local-ops-mcp..."
notion-local-ops-mcp >"${SERVER_LOG}" 2>&1 &
SERVER_PID=$!

if ! wait_for_server; then
  echo "MCP server did not become ready. Recent log output:" >&2
  tail -n 40 "${SERVER_LOG}" >&2 || true
  exit 1
fi

echo "MCP streamable HTTP endpoint: ${SERVER_URL}/mcp"
echo "Workspace root: ${NOTION_LOCAL_OPS_WORKSPACE_ROOT}"
echo "Server log: ${SERVER_LOG}"

if CLOUDFLARED_CONFIG="$(pick_cloudflared_config)"; then
  if [[ ! -f "${CLOUDFLARED_CONFIG}" ]]; then
    echo "cloudflared config not found: ${CLOUDFLARED_CONFIG}" >&2
    exit 1
  fi

  echo "Starting named cloudflared tunnel. Press Ctrl+C to stop both processes."
  echo "cloudflared config: ${CLOUDFLARED_CONFIG}"

  if [[ -n "${NOTION_LOCAL_OPS_TUNNEL_NAME:-}" ]]; then
    cloudflared tunnel --config "${CLOUDFLARED_CONFIG}" run "${NOTION_LOCAL_OPS_TUNNEL_NAME}"
  else
    cloudflared tunnel --config "${CLOUDFLARED_CONFIG}" run
  fi
else
  echo "Starting cloudflared quick tunnel. Press Ctrl+C to stop both processes."
  cloudflared tunnel --url "${SERVER_URL}"
fi
