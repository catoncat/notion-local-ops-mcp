from __future__ import annotations

from dataclasses import dataclass
import plistlib
from pathlib import Path
from typing import Mapping, Any

DEFAULT_LAUNCHD_LABEL_PREFIX = "com.notion-local-ops"
DEFAULT_LAUNCHD_LOG_DIRNAME = "notion-local-ops-mcp"
DEFAULT_MCP_MAX_FILES = 4096


@dataclass(frozen=True)
class LaunchdServiceConfig:
    repo_root: Path
    launch_agents_dir: Path
    logs_dir: Path
    label_prefix: str
    python_bin: Path
    cloudflared_bin: Path
    cloudflared_config: Path
    tunnel_name: str | None
    env: Mapping[str, str]


def mcp_service_label(label_prefix: str = DEFAULT_LAUNCHD_LABEL_PREFIX) -> str:
    return f"{label_prefix}.mcp"


def cloudflared_service_label(label_prefix: str = DEFAULT_LAUNCHD_LABEL_PREFIX) -> str:
    return f"{label_prefix}.cloudflared"


def plist_path(launch_agents_dir: Path, label: str) -> Path:
    return launch_agents_dir / f"{label}.plist"


def _base_launch_agent(
    *,
    label: str,
    working_directory: Path,
    stdout_path: Path,
    stderr_path: Path,
    program_arguments: list[str],
    environment: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "Label": label,
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(working_directory),
        "ProgramArguments": program_arguments,
        "EnvironmentVariables": dict(environment),
        "ProcessType": "Background",
        "ThrottleInterval": 5,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }


def build_mcp_launch_agent(config: LaunchdServiceConfig) -> dict[str, Any]:
    state_dir = Path(config.env["NOTION_LOCAL_OPS_STATE_DIR"])
    environment = {
        key: value
        for key, value in config.env.items()
        if value is not None and value != ""
    }
    payload = _base_launch_agent(
        label=mcp_service_label(config.label_prefix),
        working_directory=config.repo_root,
        stdout_path=config.logs_dir / "mcp.stdout.log",
        stderr_path=config.logs_dir / "mcp.stderr.log",
        program_arguments=[
            str(config.python_bin),
            "-m",
            "notion_local_ops_mcp.supervisor",
            "--pid-file",
            str(state_dir / "launchd-supervisor.pid"),
            "--log-file",
            str(config.logs_dir / "mcp-server.log"),
        ],
        environment=environment,
    )
    payload["SoftResourceLimits"] = {"NumberOfFiles": DEFAULT_MCP_MAX_FILES}
    payload["HardResourceLimits"] = {"NumberOfFiles": DEFAULT_MCP_MAX_FILES}
    return payload


def build_cloudflared_launch_agent(config: LaunchdServiceConfig) -> dict[str, Any]:
    arguments = [
        str(config.cloudflared_bin),
        "tunnel",
        "--config",
        str(config.cloudflared_config),
        "run",
    ]
    if config.tunnel_name:
        arguments.append(config.tunnel_name)
    return _base_launch_agent(
        label=cloudflared_service_label(config.label_prefix),
        working_directory=config.repo_root,
        stdout_path=config.logs_dir / "cloudflared.stdout.log",
        stderr_path=config.logs_dir / "cloudflared.stderr.log",
        program_arguments=arguments,
        environment={"PATH": config.env["PATH"]},
    )


def write_launch_agent(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(dict(payload), sort_keys=False))
