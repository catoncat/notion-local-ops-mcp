[CmdletBinding()]
param(
    [switch]$Help
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RootDir

function Show-Usage {
    @'
Usage: .\scripts\dev-tunnel.ps1

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

If .\cloudflared.local.yml or .\cloudflared.local.yaml exists, this script
uses that named tunnel config automatically. Otherwise it falls back to a
cloudflared quick tunnel.
'@
}

function Get-EnvSnapshot {
    $names = @(
        "NOTION_LOCAL_OPS_HOST",
        "NOTION_LOCAL_OPS_PORT",
        "NOTION_LOCAL_OPS_WORKSPACE_ROOT",
        "NOTION_LOCAL_OPS_STATE_DIR",
        "NOTION_LOCAL_OPS_AUTH_TOKEN",
        "NOTION_LOCAL_OPS_CLOUDFLARED_CONFIG",
        "NOTION_LOCAL_OPS_TUNNEL_NAME",
        "NOTION_LOCAL_OPS_CODEX_COMMAND",
        "NOTION_LOCAL_OPS_CLAUDE_COMMAND",
        "NOTION_LOCAL_OPS_COMMAND_TIMEOUT",
        "NOTION_LOCAL_OPS_DELEGATE_TIMEOUT"
    )

    $snapshot = @{}
    foreach ($name in $names) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if (-not [string]::IsNullOrEmpty($value)) {
            $snapshot[$name] = $value
        }
    }
    return $snapshot
}

function Import-EnvFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }

        $separator = $trimmed.IndexOf("=")
        if ($separator -lt 1) {
            continue
        }

        $name = $trimmed.Substring(0, $separator).Trim()
        $value = $trimmed.Substring($separator + 1)
        if (
            ($value.Length -ge 2) -and
            (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))
            )
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        Set-Item -Path "Env:$name" -Value $value
    }
}

function Restore-Overrides {
    param([hashtable]$Snapshot)

    foreach ($entry in $Snapshot.GetEnumerator()) {
        Set-Item -Path "Env:$($entry.Key)" -Value $entry.Value
    }
}

function Require-Command {
    param([string]$Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $Name"
    }
}

function Get-CloudflaredCommand {
    $localBinary = Join-Path $RootDir "tools\cloudflared.exe"
    if (Test-Path -LiteralPath $localBinary) {
        return $localBinary
    }

    $command = Get-Command "cloudflared" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    throw "Missing required command: cloudflared. Install it or place tools\\cloudflared.exe in the repo."
}

function Get-PythonLauncher {
    $candidates = @(
        @{ Command = "python"; PrefixArgs = @() },
        @{ Command = "py"; PrefixArgs = @("-3.11") },
        @{ Command = "py"; PrefixArgs = @("-3") }
    )

    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Command -ErrorAction SilentlyContinue
        if (-not $command) {
            continue
        }

        $args = @($candidate.PrefixArgs + "-c" + "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)")
        & $command.Source @args
        if ($LASTEXITCODE -eq 0) {
            return @{
                Command = $command.Source
                PrefixArgs = @($candidate.PrefixArgs)
            }
        }
    }

    throw "Python 3.11+ is required but no suitable interpreter was found."
}

function Resolve-RepoPath {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }

    if ([System.IO.Path]::IsPathRooted($Value)) {
        return $Value
    }

    return (Join-Path $RootDir $Value)
}

function Get-CloudflaredConfig {
    if (-not [string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_CLOUDFLARED_CONFIG)) {
        return (Resolve-RepoPath $env:NOTION_LOCAL_OPS_CLOUDFLARED_CONFIG)
    }

    foreach ($candidate in @(
        (Join-Path $RootDir "cloudflared.local.yml"),
        (Join-Path $RootDir "cloudflared.local.yaml")
    )) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    return $null
}

function Wait-ForServer {
    param(
        [string]$ListenHost,
        [int]$Port,
        [int]$TimeoutSeconds = 15
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $asyncResult = $client.BeginConnect($ListenHost, $Port, $null, $null)
            if ($asyncResult.AsyncWaitHandle.WaitOne(500)) {
                $client.EndConnect($asyncResult)
                return $true
            }
        } catch {
        } finally {
            $client.Dispose()
        }

        Start-Sleep -Milliseconds 200
    }

    return $false
}

if ($Help) {
    Show-Usage
    exit 0
}

$cloudflaredCommand = Get-CloudflaredCommand

$python = Get-PythonLauncher
$venvPython = Join-Path $RootDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $python.Command @($python.PrefixArgs + "-m" + "venv" + (Join-Path $RootDir ".venv"))
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Virtual environment creation failed: $venvPython not found."
}

& $venvPython "-c" "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.11+ is required."
}

try {
    & $venvPython "-c" "import fastmcp, uvicorn, notion_local_ops_mcp"
    $needsInstall = $LASTEXITCODE -ne 0
} catch {
    $needsInstall = $true
}

if ($needsInstall) {
    & $venvPython "-m" "pip" "install" "-r" "requirements.txt"
    & $venvPython "-m" "pip" "install" "-e" "."
}

$overrides = Get-EnvSnapshot
Import-EnvFile -Path (Join-Path $RootDir ".env")
Restore-Overrides -Snapshot $overrides

if ([string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_HOST)) {
    $env:NOTION_LOCAL_OPS_HOST = "127.0.0.1"
}
if ([string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_PORT)) {
    $env:NOTION_LOCAL_OPS_PORT = "8766"
}
if ([string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_WORKSPACE_ROOT)) {
    $env:NOTION_LOCAL_OPS_WORKSPACE_ROOT = $RootDir
}
if ([string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_AUTH_TOKEN)) {
    throw "Missing NOTION_LOCAL_OPS_AUTH_TOKEN. Set it in .env or export it before running."
}

$serverUrl = "http://$($env:NOTION_LOCAL_OPS_HOST):$($env:NOTION_LOCAL_OPS_PORT)"
$serverStdout = Join-Path ([System.IO.Path]::GetTempPath()) "notion-local-ops-mcp-server.$PID.stdout.log"
$serverStderr = Join-Path ([System.IO.Path]::GetTempPath()) "notion-local-ops-mcp-server.$PID.stderr.log"
$serverProcess = $null

try {
    Write-Host "Starting notion-local-ops-mcp..."
    $serverProcess = Start-Process `
        -FilePath $venvPython `
        -ArgumentList @("-m", "notion_local_ops_mcp.server") `
        -WorkingDirectory $RootDir `
        -RedirectStandardOutput $serverStdout `
        -RedirectStandardError $serverStderr `
        -PassThru

    if (-not (Wait-ForServer -ListenHost $env:NOTION_LOCAL_OPS_HOST -Port ([int]$env:NOTION_LOCAL_OPS_PORT))) {
        Write-Error "MCP server did not become ready. Recent log output:"
        if (Test-Path -LiteralPath $serverStdout) {
            Get-Content -LiteralPath $serverStdout -Tail 40 | Write-Error
        }
        if (Test-Path -LiteralPath $serverStderr) {
            Get-Content -LiteralPath $serverStderr -Tail 40 | Write-Error
        }
        exit 1
    }

    Write-Host "MCP streamable HTTP endpoint: $serverUrl/mcp"
    Write-Host "Workspace root: $($env:NOTION_LOCAL_OPS_WORKSPACE_ROOT)"
    Write-Host "Server stdout log: $serverStdout"
    Write-Host "Server stderr log: $serverStderr"

    $cloudflaredConfig = Get-CloudflaredConfig
    if ($cloudflaredConfig) {
        if (-not (Test-Path -LiteralPath $cloudflaredConfig)) {
            throw "cloudflared config not found: $cloudflaredConfig"
        }

        Write-Host "Starting named cloudflared tunnel. Press Ctrl+C to stop both processes."
        Write-Host "cloudflared config: $cloudflaredConfig"

        if (-not [string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_TUNNEL_NAME)) {
            & $cloudflaredCommand tunnel --config $cloudflaredConfig run $env:NOTION_LOCAL_OPS_TUNNEL_NAME
        } else {
            & $cloudflaredCommand tunnel --config $cloudflaredConfig run
        }
    } else {
        Write-Host "Starting cloudflared quick tunnel. Press Ctrl+C to stop both processes."
        & $cloudflaredCommand tunnel --url $serverUrl
    }
} finally {
    if ($serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id -Force
        $serverProcess.WaitForExit()
    }
}
