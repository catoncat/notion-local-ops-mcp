from __future__ import annotations

from pathlib import Path

from notion_local_ops_mcp.launchd_support import (
    LaunchdServiceConfig,
    build_cloudflared_launch_agent,
    build_mcp_launch_agent,
    mcp_service_label,
    cloudflared_service_label,
    plist_path,
)


def _config(tmp_path: Path) -> LaunchdServiceConfig:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    return LaunchdServiceConfig(
        repo_root=repo_root,
        launch_agents_dir=tmp_path / "LaunchAgents",
        logs_dir=tmp_path / "logs",
        label_prefix="com.example.notion-local-ops",
        python_bin=repo_root / ".venv" / "bin" / "python",
        cloudflared_bin=Path("/opt/homebrew/bin/cloudflared"),
        cloudflared_config=repo_root / "cloudflared.local.yml",
        tunnel_name="named-tunnel",
        env={
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
            "NOTION_LOCAL_OPS_HOST": "127.0.0.1",
            "NOTION_LOCAL_OPS_PORT": "8766",
            "NOTION_LOCAL_OPS_WORKSPACE_ROOT": "/tmp/workspace",
            "NOTION_LOCAL_OPS_STATE_DIR": "/tmp/state",
            "NOTION_LOCAL_OPS_AUTH_TOKEN": "secret-token",
            "NOTION_LOCAL_OPS_AUTH_MODE": "oauth",
            "NOTION_LOCAL_OPS_PUBLIC_BASE_URL": "https://mcp.example.test",
            "NOTION_LOCAL_OPS_OAUTH_SCOPES": "local-ops",
            "NOTION_LOCAL_OPS_OAUTH_TOKEN_TTL_SECONDS": "86400",
            "NOTION_LOCAL_OPS_CODEX_COMMAND": "codex",
            "NOTION_LOCAL_OPS_CLAUDE_COMMAND": "claude",
            "NOTION_LOCAL_OPS_COMMAND_TIMEOUT": "120",
            "NOTION_LOCAL_OPS_DELEGATE_TIMEOUT": "1800",
            "NOTION_LOCAL_OPS_DEBUG_MCP_LOGGING": "1",
            "NOTION_LOCAL_OPS_GRACEFUL_SHUTDOWN_SECONDS": "30",
        },
    )


def test_build_mcp_launch_agent_contains_supervisor_and_runtime_env(tmp_path: Path) -> None:
    config = _config(tmp_path)

    payload = build_mcp_launch_agent(config)

    assert payload["Label"] == "com.example.notion-local-ops.mcp"
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["WorkingDirectory"] == str(config.repo_root)
    assert payload["ProgramArguments"] == [
        str(config.python_bin),
        "-m",
        "notion_local_ops_mcp.supervisor",
        "--pid-file",
        str(Path("/tmp/state") / "launchd-supervisor.pid"),
        "--log-file",
        str(config.logs_dir / "mcp-server.log"),
    ]
    assert payload["EnvironmentVariables"]["NOTION_LOCAL_OPS_AUTH_TOKEN"] == "secret-token"
    assert payload["EnvironmentVariables"]["NOTION_LOCAL_OPS_AUTH_MODE"] == "oauth"
    assert payload["EnvironmentVariables"]["NOTION_LOCAL_OPS_PUBLIC_BASE_URL"] == "https://mcp.example.test"
    assert payload["EnvironmentVariables"]["PATH"] == "/opt/homebrew/bin:/usr/bin:/bin"
    assert payload["StandardOutPath"] == str(config.logs_dir / "mcp.stdout.log")
    assert payload["StandardErrorPath"] == str(config.logs_dir / "mcp.stderr.log")
    assert payload["SoftResourceLimits"] == {"NumberOfFiles": 4096}
    assert payload["HardResourceLimits"] == {"NumberOfFiles": 4096}


def test_build_cloudflared_launch_agent_uses_named_tunnel_when_present(tmp_path: Path) -> None:
    config = _config(tmp_path)

    payload = build_cloudflared_launch_agent(config)

    assert payload["Label"] == "com.example.notion-local-ops.cloudflared"
    assert payload["ProgramArguments"] == [
        "/opt/homebrew/bin/cloudflared",
        "tunnel",
        "--config",
        str(config.cloudflared_config),
        "run",
        "named-tunnel",
    ]
    assert payload["EnvironmentVariables"] == {"PATH": "/opt/homebrew/bin:/usr/bin:/bin"}
    assert payload["StandardOutPath"] == str(config.logs_dir / "cloudflared.stdout.log")
    assert payload["StandardErrorPath"] == str(config.logs_dir / "cloudflared.stderr.log")


def test_launchd_label_helpers_and_plist_path(tmp_path: Path) -> None:
    launch_agents_dir = tmp_path / "LaunchAgents"
    prefix = "com.example.notion-local-ops"

    assert mcp_service_label(prefix) == "com.example.notion-local-ops.mcp"
    assert cloudflared_service_label(prefix) == "com.example.notion-local-ops.cloudflared"
    assert plist_path(launch_agents_dir, mcp_service_label(prefix)) == (
        launch_agents_dir / "com.example.notion-local-ops.mcp.plist"
    )
