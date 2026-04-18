[CmdletBinding()]
param(
    [switch]$Restart,
    [switch]$NoClipboard,
    [int]$FirstPort,
    [int]$SecondPort,
    [string]$FirstToken,
    [string]$SecondToken,
    [string]$WorkspaceRoot
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RootDir

$StateDir = Join-Path $RootDir ".state"
$StatePath = Join-Path $StateDir "quick-dual-tunnel-state.json"
$TracePath = Join-Path $StateDir "quick-start-dual.trace.log"

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

    $State | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $StatePath -Encoding UTF8
}

function Stop-StateProcesses {
    param($State)

    if (-not $State) {
        return
    }

    foreach ($instance in @($State.instances)) {
        foreach ($managedProcessId in @($instance.server_pid, $instance.cloudflared_pid)) {
            if ($managedProcessId -and (Test-ProcessAlive -ProcessId $managedProcessId)) {
                try {
                    Stop-Process -Id $managedProcessId -Force
                } catch {
                }
            }
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

function New-RandomToken {
    return [guid]::NewGuid().ToString("N")
}

function Set-LaunchEnvironment {
    param([hashtable]$Values)

    $snapshot = @{}
    foreach ($name in $Values.Keys) {
        $currentValue = [Environment]::GetEnvironmentVariable($name, "Process")
        if ($null -eq $currentValue) {
            $snapshot[$name] = $null
        } else {
            $snapshot[$name] = $currentValue
        }

        if ([string]::IsNullOrEmpty([string]$Values[$name])) {
            Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$name" -Value ([string]$Values[$name])
        }
    }

    return $snapshot
}

function Restore-LaunchEnvironment {
    param([hashtable]$Snapshot)

    foreach ($entry in $Snapshot.GetEnumerator()) {
        if ($null -eq $entry.Value) {
            Remove-Item -Path "Env:$($entry.Key)" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$($entry.Key)" -Value ([string]$entry.Value)
        }
    }
}

function Assert-PortFree {
    param([int]$Port)

    $existing = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
    if ($existing) {
        throw "Port $Port is already in use. Stop the conflicting process first."
    }
}

function Start-McpInstance {
    param(
        [string]$Name,
        [string]$ListenHost,
        [int]$Port,
        [string]$Token,
        [string]$WorkspaceRootPath,
        [string]$VenvPython,
        [string]$CloudflaredCommand
    )

    $instanceDir = Join-Path $StateDir $Name
    $instanceStateDir = Join-Path $instanceDir "mcp-state"
    New-Item -ItemType Directory -Force -Path $instanceDir | Out-Null
    New-Item -ItemType Directory -Force -Path $instanceStateDir | Out-Null

    $serverStdout = Join-Path $instanceDir "server.stdout.log"
    $serverStderr = Join-Path $instanceDir "server.stderr.log"
    $cloudflaredStdout = Join-Path $instanceDir "cloudflared.stdout.log"
    $cloudflaredStderr = Join-Path $instanceDir "cloudflared.stderr.log"
    foreach ($path in @($serverStdout, $serverStderr, $cloudflaredStdout, $cloudflaredStderr)) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
        }
    }

    $launchEnv = @{
        NOTION_LOCAL_OPS_HOST = $ListenHost
        NOTION_LOCAL_OPS_PORT = [string]$Port
        NOTION_LOCAL_OPS_WORKSPACE_ROOT = $WorkspaceRootPath
        NOTION_LOCAL_OPS_STATE_DIR = $instanceStateDir
        NOTION_LOCAL_OPS_AUTH_TOKEN = $Token
        NOTION_LOCAL_OPS_CODEX_COMMAND = $env:NOTION_LOCAL_OPS_CODEX_COMMAND
        NOTION_LOCAL_OPS_CLAUDE_COMMAND = $env:NOTION_LOCAL_OPS_CLAUDE_COMMAND
        NOTION_LOCAL_OPS_COMMAND_TIMEOUT = $env:NOTION_LOCAL_OPS_COMMAND_TIMEOUT
        NOTION_LOCAL_OPS_DELEGATE_TIMEOUT = $env:NOTION_LOCAL_OPS_DELEGATE_TIMEOUT
    }

    $serverProcess = $null
    $cloudflaredProcess = $null
    $serverUrl = "http://$($ListenHost):$Port"

    $snapshot = Set-LaunchEnvironment -Values $launchEnv
    try {
        Write-TraceLine "starting instance $Name on $serverUrl"
        $serverProcess = Start-Process `
            -FilePath $VenvPython `
            -ArgumentList @("-m", "notion_local_ops_mcp.server") `
            -WorkingDirectory $RootDir `
            -RedirectStandardOutput $serverStdout `
            -RedirectStandardError $serverStderr `
            -PassThru
    } finally {
        Restore-LaunchEnvironment -Snapshot $snapshot
    }

    if (-not (Wait-ForPort -ListenHost $ListenHost -Port $Port -TimeoutSeconds 20)) {
        if ($serverProcess -and (Test-ProcessAlive -ProcessId $serverProcess.Id)) {
            Stop-Process -Id $serverProcess.Id -Force
        }
        throw "Instance $Name did not become ready on $serverUrl"
    }

    $cloudflaredProcess = Start-Process `
        -FilePath $CloudflaredCommand `
        -ArgumentList @("tunnel", "--url", $serverUrl) `
        -WorkingDirectory $RootDir `
        -RedirectStandardOutput $cloudflaredStdout `
        -RedirectStandardError $cloudflaredStderr `
        -PassThru

    $quickTunnelUrl = Wait-ForQuickTunnelUrl -LogPath $cloudflaredStderr -TimeoutSeconds 45
    if (-not $quickTunnelUrl) {
        if ($cloudflaredProcess -and (Test-ProcessAlive -ProcessId $cloudflaredProcess.Id)) {
            Stop-Process -Id $cloudflaredProcess.Id -Force
        }
        if ($serverProcess -and (Test-ProcessAlive -ProcessId $serverProcess.Id)) {
            Stop-Process -Id $serverProcess.Id -Force
        }
        throw "Failed to extract quick tunnel URL for instance $Name from $cloudflaredStderr"
    }

    return [ordered]@{
        name = $Name
        host = $ListenHost
        port = $Port
        token = $Token
        local_url = "$serverUrl/mcp"
        public_url = $quickTunnelUrl
        mcp_url = "$quickTunnelUrl/mcp"
        server_pid = $serverProcess.Id
        cloudflared_pid = $cloudflaredProcess.Id
        state_dir = $instanceStateDir
        server_stdout_log = $serverStdout
        server_stderr_log = $serverStderr
        cloudflared_stdout_log = $cloudflaredStdout
        cloudflared_stderr_log = $cloudflaredStderr
    }
}

function Format-InstanceSummary {
    param($Instance)

    return @(
        "$($Instance.name):"
        "  MCP URL: $($Instance.mcp_url)"
        "  Token: $($Instance.token)"
        "  Local URL: $($Instance.local_url)"
        "  Server PID: $($Instance.server_pid)"
        "  cloudflared PID: $($Instance.cloudflared_pid)"
    ) -join [Environment]::NewLine
}

Ensure-StateDir
if (Test-Path -LiteralPath $TracePath) {
    Remove-Item -LiteralPath $TracePath -Force
}
Write-TraceLine "quick-start-dual begin"

Import-EnvFile -Path (Join-Path $RootDir ".env")
Write-TraceLine "env loaded"

$listenHost = if ([string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_HOST)) { "127.0.0.1" } else { $env:NOTION_LOCAL_OPS_HOST }
$workspaceRootPath = if ([string]::IsNullOrWhiteSpace($WorkspaceRoot)) {
    if ([string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_WORKSPACE_ROOT)) { $RootDir } else { $env:NOTION_LOCAL_OPS_WORKSPACE_ROOT }
} else {
    $WorkspaceRoot
}

if ($FirstPort -le 0) {
    if ([string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_PORT)) {
        $FirstPort = 8766
    } else {
        $FirstPort = [int]$env:NOTION_LOCAL_OPS_PORT
    }
}

if ($SecondPort -le 0) {
    if ([string]::IsNullOrWhiteSpace($env:NOTION_LOCAL_OPS_SECOND_PORT)) {
        $SecondPort = $FirstPort + 1
    } else {
        $SecondPort = [int]$env:NOTION_LOCAL_OPS_SECOND_PORT
    }
}

if ($FirstPort -eq $SecondPort) {
    throw "FirstPort and SecondPort must be different."
}

$existingState = Read-State

if ([string]::IsNullOrWhiteSpace($FirstToken)) {
    $FirstToken = $env:NOTION_LOCAL_OPS_AUTH_TOKEN
}
if ([string]::IsNullOrWhiteSpace($FirstToken)) {
    throw "Missing first bearer token. Set NOTION_LOCAL_OPS_AUTH_TOKEN in .env or pass -FirstToken."
}

if ([string]::IsNullOrWhiteSpace($SecondToken)) {
    $SecondToken = $env:NOTION_LOCAL_OPS_AUTH_TOKEN_SECOND
}
if ([string]::IsNullOrWhiteSpace($SecondToken) -and $existingState -and $existingState.instances.Count -ge 2) {
    $SecondToken = $existingState.instances[1].token
}
if ([string]::IsNullOrWhiteSpace($SecondToken)) {
    $SecondToken = New-RandomToken
    Write-TraceLine "generated second bearer token"
}

if ($existingState -and -not $Restart) {
    $allAlive = $true
    foreach ($instance in @($existingState.instances)) {
        if (-not (Test-ProcessAlive -ProcessId ([int]$instance.server_pid))) {
            $allAlive = $false
        }
        if (-not (Test-ProcessAlive -ProcessId ([int]$instance.cloudflared_pid))) {
            $allAlive = $false
        }
    }

    if ($allAlive) {
        $summary = (@($existingState.instances) | ForEach-Object { Format-InstanceSummary -Instance $_ }) -join ([Environment]::NewLine + [Environment]::NewLine)
        Write-Host "Dual quick tunnels are already running."
        Write-Host ""
        Write-Host $summary
        Write-Host ""
        Write-Host "State file: $StatePath"
        if (-not $NoClipboard) {
            Set-Clipboard -Value $summary
            Write-Host "Summary copied to clipboard."
        }
        exit 0
    }
}

if ($existingState) {
    Write-TraceLine "stopping existing dual state processes"
    Stop-StateProcesses -State $existingState
}

Assert-PortFree -Port $FirstPort
Assert-PortFree -Port $SecondPort

$venvPython = Ensure-Dependencies
$cloudflaredCommand = Get-CloudflaredCommand

$startedInstances = New-Object System.Collections.Generic.List[object]
try {
    $firstInstance = Start-McpInstance `
        -Name "agent-a" `
        -ListenHost $listenHost `
        -Port $FirstPort `
        -Token $FirstToken `
        -WorkspaceRootPath $workspaceRootPath `
        -VenvPython $venvPython `
        -CloudflaredCommand $cloudflaredCommand
    [void]$startedInstances.Add($firstInstance)

    $secondInstance = Start-McpInstance `
        -Name "agent-b" `
        -ListenHost $listenHost `
        -Port $SecondPort `
        -Token $SecondToken `
        -WorkspaceRootPath $workspaceRootPath `
        -VenvPython $venvPython `
        -CloudflaredCommand $cloudflaredCommand
    [void]$startedInstances.Add($secondInstance)
} catch {
    foreach ($instance in $startedInstances) {
        foreach ($managedProcessId in @($instance.server_pid, $instance.cloudflared_pid)) {
            if ($managedProcessId -and (Test-ProcessAlive -ProcessId $managedProcessId)) {
                try {
                    Stop-Process -Id $managedProcessId -Force
                } catch {
                }
            }
        }
    }
    throw
}

$instances = @($startedInstances[0], $startedInstances[1])
Write-State @{
    started_at = (Get-Date).ToString("o")
    workspace_root = $workspaceRootPath
    instances = $instances
}
Write-TraceLine "dual state file written"

$summary = ($instances | ForEach-Object { Format-InstanceSummary -Instance $_ }) -join ([Environment]::NewLine + [Environment]::NewLine)
if (-not $NoClipboard) {
    Set-Clipboard -Value $summary
    Write-TraceLine "clipboard updated"
}

Write-TraceLine "quick-start-dual success"
Write-Host ""
Write-Host "Dual quick tunnels are ready."
Write-Host ""
Write-Host $summary
Write-Host ""
Write-Host "State file: $StatePath"
Write-Host "Copied to clipboard: $([bool](-not $NoClipboard))"
Write-Host "Use .\scripts\quick-stop-dual.ps1 to stop both."
