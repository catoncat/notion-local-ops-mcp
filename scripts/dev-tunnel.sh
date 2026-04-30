#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ACTION="${1:-start}"
SUPERVISOR_PID=""
SERVER_LOG=""

usage() {
  cat <<'EOF'
Usage:
  ./scripts/dev-tunnel.sh            # start supervisor + local MCP server + cloudflared tunnel
  ./scripts/dev-tunnel.sh start      # same as above
  ./scripts/dev-tunnel.sh reload     # rolling-reload the local MCP server without dropping the tunnel
  ./scripts/dev-tunnel.sh status     # show supervisor / endpoint status
  ./scripts/dev-tunnel.sh --help

Environment loading order:
1. .env in the repository root
2. Current shell environment overrides matching keys

Required for start:
- NOTION_LOCAL_OPS_AUTH_TOKEN

Optional:
- NOTION_LOCAL_OPS_WORKSPACE_ROOT (defaults to repo root)
- NOTION_LOCAL_OPS_HOST (defaults to 127.0.0.1)
- NOTION_LOCAL_OPS_PORT (defaults to 8766)
- NOTION_LOCAL_OPS_STATE_DIR (defaults to ~/.notion-local-ops-mcp)
- NOTION_LOCAL_OPS_CLOUDFLARED_CONFIG (named tunnel config path)
- NOTION_LOCAL_OPS_TUNNEL_NAME (optional override for cloudflared tunnel run)
- NOTION_LOCAL_OPS_DEBUG_MCP_LOGGING (set to 1/true/on to log MCP methods/tools)
- NOTION_LOCAL_OPS_GRACEFUL_SHUTDOWN_SECONDS (drain time during rolling reload; default 30)

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
  if [[ -n "${SUPERVISOR_PID:-}" ]] && kill -0 "${SUPERVISOR_PID}" >/dev/null 2>&1; then
    kill "${SUPERVISOR_PID}" >/dev/null 2>&1 || true
    wait "${SUPERVISOR_PID}" 2>/dev/null || true
  fi
  exit "${exit_code}"
}

supervisor_pid() {
  if [[ ! -f "${SUPERVISOR_PID_FILE}" ]]; then
    return 1
  fi
  tr -d '[:space:]' <"${SUPERVISOR_PID_FILE}"
}

supervisor_running() {
  local pid
  pid="$(supervisor_pid 2>/dev/null || true)"
  if [[ -z "${pid}" ]]; then
    return 1
  fi
  if ! kill -0 "${pid}" >/dev/null 2>&1; then
    return 1
  fi
  printf '%s\n' "${pid}"
}

print_status() {
  local pid
  if pid="$(supervisor_running)"; then
    echo "Supervisor running: pid=${pid}"
    ps -o pid,ppid,lstart,etime,command -p "${pid}"
  else
    echo "Supervisor not running"
  fi

  echo "Endpoint: http://${NOTION_LOCAL_OPS_HOST}:${NOTION_LOCAL_OPS_PORT}/mcp"
  if command -v curl >/dev/null 2>&1; then
    if curl -fsSI "http://${NOTION_LOCAL_OPS_HOST}:${NOTION_LOCAL_OPS_PORT}/mcp" >/dev/null 2>&1; then
      echo "Local MCP endpoint is reachable"
    else
      echo "Local MCP endpoint is not reachable"
    fi
  fi

  if [[ -n "${SERVER_LOG:-}" ]]; then
    echo "Current server log: ${SERVER_LOG}"
  fi
  echo "Supervisor pid file: ${SUPERVISOR_PID_FILE}"
}

if [[ "${ACTION}" == "--help" || "${ACTION}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ $# -gt 1 ]]; then
  usage >&2
  exit 1
fi

if [[ "${ACTION}" != "start" && "${ACTION}" != "reload" && "${ACTION}" != "status" ]]; then
  usage >&2
  exit 1
fi

PYTHON_BIN="$(pick_python)"

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
OVERRIDE_DEBUG_MCP_LOGGING="${NOTION_LOCAL_OPS_DEBUG_MCP_LOGGING:-}"
OVERRIDE_GRACEFUL_SHUTDOWN_SECONDS="${NOTION_LOCAL_OPS_GRACEFUL_SHUTDOWN_SECONDS:-}"

load_env_file

export NOTION_LOCAL_OPS_HOST="${OVERRIDE_HOST:-${NOTION_LOCAL_OPS_HOST:-127.0.0.1}}"
export NOTION_LOCAL_OPS_PORT="${OVERRIDE_PORT:-${NOTION_LOCAL_OPS_PORT:-8766}}"
export NOTION_LOCAL_OPS_WORKSPACE_ROOT="${OVERRIDE_WORKSPACE_ROOT:-${NOTION_LOCAL_OPS_WORKSPACE_ROOT:-${ROOT_DIR}}}"
export NOTION_LOCAL_OPS_STATE_DIR="${OVERRIDE_STATE_DIR:-${NOTION_LOCAL_OPS_STATE_DIR:-${HOME}/.notion-local-ops-mcp}}"

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

if [[ -n "${OVERRIDE_DEBUG_MCP_LOGGING}" ]]; then
  export NOTION_LOCAL_OPS_DEBUG_MCP_LOGGING="${OVERRIDE_DEBUG_MCP_LOGGING}"
fi

if [[ -n "${OVERRIDE_GRACEFUL_SHUTDOWN_SECONDS}" ]]; then
  export NOTION_LOCAL_OPS_GRACEFUL_SHUTDOWN_SECONDS="${OVERRIDE_GRACEFUL_SHUTDOWN_SECONDS}"
fi

SUPERVISOR_PID_FILE="${NOTION_LOCAL_OPS_STATE_DIR}/dev-tunnel-supervisor.pid"
SERVER_LOG="$(ls -1t ${TMPDIR:-/tmp}/notion-local-ops-mcp-server.*.log 2>/dev/null | head -n1 || true)"

case "${ACTION}" in
  reload)
    if ! pid="$(supervisor_running)"; then
      echo "No running dev-tunnel supervisor found. Start one with ./scripts/dev-tunnel.sh" >&2
      exit 1
    fi
    kill -HUP "${pid}"
    echo "Sent rolling-reload signal to supervisor pid=${pid}"
    exit 0
    ;;
  status)
    print_status
    exit 0
    ;;
  start)
    ;;
esac

trap cleanup EXIT INT TERM

require_command cloudflared
if [[ ! -d "${ROOT_DIR}/.venv" ]]; then
  "${PYTHON_BIN}" -m venv "${ROOT_DIR}/.venv"
fi

# shellcheck disable=SC1091
source "${ROOT_DIR}/.venv/bin/activate"

ensure_python_runtime_deps

if [[ -z "${NOTION_LOCAL_OPS_AUTH_TOKEN:-}" ]]; then
  echo "Missing NOTION_LOCAL_OPS_AUTH_TOKEN. Set it in .env or export it before running." >&2
  exit 1
fi

if pid="$(supervisor_running)"; then
  echo "A dev-tunnel supervisor is already running (pid=${pid})." >&2
  echo "Use ./scripts/dev-tunnel.sh reload to restart the local MCP server without dropping the tunnel." >&2
  exit 1
fi

SERVER_URL="http://${NOTION_LOCAL_OPS_HOST}:${NOTION_LOCAL_OPS_PORT}"
SERVER_LOG="${TMPDIR:-/tmp}/notion-local-ops-mcp-server.$$.log"

echo "Starting notion-local-ops-mcp supervisor..."
python -m notion_local_ops_mcp.supervisor \
  --pid-file "${SUPERVISOR_PID_FILE}" \
  --log-file "${SERVER_LOG}" &
SUPERVISOR_PID=$!

if ! wait_for_server; then
  echo "MCP server did not become ready. Recent log output:" >&2
  tail -n 40 "${SERVER_LOG}" >&2 || true
  exit 1
fi

echo "MCP endpoint: ${SERVER_URL}/mcp"
echo "Workspace root: ${NOTION_LOCAL_OPS_WORKSPACE_ROOT}"
echo "State dir: ${NOTION_LOCAL_OPS_STATE_DIR}"
echo "Supervisor pid: ${SUPERVISOR_PID}"
echo "Supervisor pid file: ${SUPERVISOR_PID_FILE}"
echo "Server log: ${SERVER_LOG}"
echo "Rolling reload command: ./scripts/dev-tunnel.sh reload"

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
