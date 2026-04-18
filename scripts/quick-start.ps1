[CmdletBinding()]
param(
    [switch]$Restart,
    [switch]$NoClipboard
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RootDir

$StateDir = Join-Path $RootDir ".state"
$StatePath = Join-Path $StateDir "quick-tunnel-state.json"
$TracePath = Join-Path $StateDir "quick-start.trace.log"

function Write-TraceLine {
    param([string]$Message)
    Add-Content -LiteralPath $TracePath -Value ("[{0}] {1}" -f (Get-Date).ToString("o"), $Message)
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

    throw "Missing required command: cloudflared. Run .\scripts\install-cloudflared.ps1 first."
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

function Ensure-StateDir {
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
}

function Test-ProcessAlive {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return $false
    }

    return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Read-State {
    if (-not (Test-Path -LiteralPath $StatePath)) {
        return $null
    }

    return Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
}

function Write-State {
    param([hashtable]$State)

    $State | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $StatePath -Encoding UTF8
}

function Stop-StateProcesses {
    param($State)

    if (-not $State) {
        return
    }

    foreach ($managedProcessId in @($State.server_pid, $State.cloudflared_pid)) {
        if ($managedProcessId -and (Test-ProcessAlive -ProcessId $managedProcessId)) {
            try {
                Stop-Process -Id $managedProcessId -Force
            } catch {
            }
        }
    }

    Start-Sleep -Milliseconds 500
}

function Stop-StrayManagedProcesses {
    $commandPattern = 'notion_local_ops_mcp\.server|cloudflared\.exe"\s+tunnel\s+--url\s+http://127\.0\.0\.1:8766'

    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -match 'python|cloudflared' -and
            $_.CommandLine -match $commandPattern
        } |
        ForEach-Object {
            try {
                Stop-Process -Id $_.ProcessId -Force
            } catch {
            }
        }

    Start-Sleep -Milliseconds 500
}

function Wait-ForPort {
    param(
        [string]$ListenHost,
        [int]$Port,
        [int]$TimeoutSeconds = 20
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

        Start-Sleep -Milliseconds 250
    }

    return $false
}

function Wait-ForQuickTunnelUrl {
    param(
        [string]$LogPath,
        [int]$TimeoutSeconds = 45
    )

    $pattern = 'https://[a-z0-9-]+\.trycloudflare\.com'
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $LogPath) {
            try {
                $content = Get-Content -LiteralPath $LogPath -Raw -ErrorAction Stop
                $match = [regex]::Match($content, $pattern)
                if ($match.Success) {
                    return $match.Value
                }
            } catch {
            }
        }

        Start-Sleep -Milliseconds 500
    }

    return $null
}

function Ensure-Dependencies {
    $python = Get-PythonLauncher
    $venvPython = Join-Path $RootDir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $python.Command @($python.PrefixArgs + "-m" + "venv" + (Join-Path $RootDir ".venv"))
    }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "Virtual environment creation failed: $venvPython not found."
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

    return $venvPython
}

Ensure-StateDir
if (Test-Path -LiteralPath $TracePath) {
    Remove-Item -LiteralPath $TracePath -Force
}
Write-TraceLine "quick-start begin"
Import-EnvFile -Path (Join-Path $RootDir ".env")
Write-TraceLine "env loaded"

if ([string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_HOST)) {
    $env:NOTION_LOCAL_OPS_HOST = "127.0.0.1"
}
if ([string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_PORT)) {
    $env:NOTION_LOCAL_OPS_PORT = "8766"
}
if ([string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_WORKSPACE_ROOT)) {
    $env:NOTION_LOCAL_OPS_WORKSPACE_ROOT = $RootDir
}
if ([string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_STATE_DIR)) {
    $env:NOTION_LOCAL_OPS_STATE_DIR = $StateDir
}
if ([string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_AUTH_TOKEN)) {
    throw "Missing NOTION_LOCAL_OPS_AUTH_TOKEN. Set it in .env first."
}

$existingState = Read-State
if ($existingState -and -not $Restart) {
    Write-TraceLine "existing state found"
    $serverAlive = Test-ProcessAlive -ProcessId ([int]$existingState.server_pid)
    $cloudflaredAlive = Test-ProcessAlive -ProcessId ([int]$existingState.cloudflared_pid)
    if ($serverAlive -and $cloudflaredAlive) {
        Write-Host "quick tunnel is already running."
        Write-Host "MCP URL: $($existingState.mcp_url)"
        Write-Host "State file: $StatePath"
        if (-not $NoClipboard -and $existingState.mcp_url) {
            Set-Clipboard -Value $existingState.mcp_url
            Write-Host "MCP URL copied to clipboard."
        }
        exit 0
    }
}

if ($existingState) {
    Write-TraceLine "stopping existing state processes"
    Stop-StateProcesses -State $existingState
}

Write-TraceLine "stopping stray managed processes"
Stop-StrayManagedProcesses

$listenPort = [int]$env:NOTION_LOCAL_OPS_PORT
$existingPortUse = Get-NetTCPConnection -LocalPort $listenPort -ErrorAction SilentlyContinue
if ($existingPortUse) {
    throw "Port $listenPort is already in use. Stop the existing process or run .\scripts\quick-stop.ps1 first."
}

$venvPython = Ensure-Dependencies
$cloudflaredCommand = Get-CloudflaredCommand
$serverUrl = "http://$($env:NOTION_LOCAL_OPS_HOST):$listenPort"
Write-TraceLine "dependencies ready"

$serverStdout = Join-Path $StateDir "quick-server.stdout.log"
$serverStderr = Join-Path $StateDir "quick-server.stderr.log"
$cloudflaredStdout = Join-Path $StateDir "quick-cloudflared.stdout.log"
$cloudflaredStderr = Join-Path $StateDir "quick-cloudflared.stderr.log"
$currentUrlPath = Join-Path $StateDir "current-mcp-url.txt"

foreach ($path in @($serverStdout, $serverStderr, $cloudflaredStdout, $cloudflaredStderr, $currentUrlPath, $StatePath)) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
}

Write-Host "Starting local MCP server..."
Write-TraceLine "starting local server"
$serverProcess = Start-Process `
    -FilePath $venvPython `
    -ArgumentList @("-m", "notion_local_ops_mcp.server") `
    -WorkingDirectory $RootDir `
    -RedirectStandardOutput $serverStdout `
    -RedirectStandardError $serverStderr `
    -PassThru

if (-not (Wait-ForPort -ListenHost $env:NOTION_LOCAL_OPS_HOST -Port $listenPort -TimeoutSeconds 20)) {
    if (Test-ProcessAlive -ProcessId $serverProcess.Id) {
        Stop-Process -Id $serverProcess.Id -Force
    }
    throw "Local MCP server did not become ready on $serverUrl"
}
Write-TraceLine "local server ready"

Write-Host "Starting cloudflared quick tunnel..."
Write-TraceLine "starting cloudflared"
$cloudflaredProcess = Start-Process `
    -FilePath $cloudflaredCommand `
    -ArgumentList @("tunnel", "--url", $serverUrl) `
    -WorkingDirectory $RootDir `
    -RedirectStandardOutput $cloudflaredStdout `
    -RedirectStandardError $cloudflaredStderr `
    -PassThru

$quickTunnelUrl = Wait-ForQuickTunnelUrl -LogPath $cloudflaredStderr -TimeoutSeconds 45
if (-not $quickTunnelUrl) {
    if (Test-ProcessAlive -ProcessId $cloudflaredProcess.Id) {
        Stop-Process -Id $cloudflaredProcess.Id -Force
    }
    if (Test-ProcessAlive -ProcessId $serverProcess.Id) {
        Stop-Process -Id $serverProcess.Id -Force
    }
    throw "Failed to extract quick tunnel URL from $cloudflaredStderr"
}
Write-TraceLine "quick tunnel url found: $quickTunnelUrl"

$mcpUrl = "$quickTunnelUrl/mcp"
Set-Content -LiteralPath $currentUrlPath -Value $mcpUrl -Encoding UTF8
Write-TraceLine "current mcp url written"

Write-State @{
    started_at = (Get-Date).ToString("o")
    server_pid = $serverProcess.Id
    cloudflared_pid = $cloudflaredProcess.Id
    local_url = "$serverUrl/mcp"
    public_url = $quickTunnelUrl
    mcp_url = $mcpUrl
    server_stdout_log = $serverStdout
    server_stderr_log = $serverStderr
    cloudflared_stdout_log = $cloudflaredStdout
    cloudflared_stderr_log = $cloudflaredStderr
    url_file = $currentUrlPath
}
Write-TraceLine "state file written"

if (-not $NoClipboard) {
    Set-Clipboard -Value $mcpUrl
    Write-TraceLine "clipboard updated"
}

Write-TraceLine "quick-start success"
Write-Host ""
Write-Host "Quick tunnel is ready."
Write-Host "MCP URL: $mcpUrl"
Write-Host "Copied to clipboard: $([bool](-not $NoClipboard))"
Write-Host "State file: $StatePath"
Write-Host "Server PID: $($serverProcess.Id)"
Write-Host "cloudflared PID: $($cloudflaredProcess.Id)"
Write-Host "Use .\scripts\quick-stop.ps1 to stop it."
