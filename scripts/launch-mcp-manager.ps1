param(
    [int]$DefaultCount = 1,
    [int]$DefaultBasePort = 8766,
    [int]$RequestedCount = 0,
    [int]$RequestedBasePort = 0,
    [switch]$NonInteractive,
    [int]$MonitorCycles = 0,
    [int]$MonitorIntervalSeconds = 5
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Net.Http

$RepoRoot = Split-Path -Parent $PSScriptRoot
$envMap = @{}
$DesktopStatusPath = Join-Path ([Environment]::GetFolderPath('Desktop')) "Notion-MCP-status.txt"

function Read-DotEnvFile {
    param([string]$Path)

    $result = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $result
    }

    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) {
            continue
        }

        $eqIndex = $trimmed.IndexOf('=')
        if ($eqIndex -lt 1) {
            continue
        }

        $key = $trimmed.Substring(0, $eqIndex).Trim()
        $value = $trimmed.Substring($eqIndex + 1).Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $result[$key] = $value
    }

    return $result
}

function Get-MergedConfigValue {
    param(
        [hashtable]$EnvMap,
        [string]$Name,
        [string]$Default = ''
    )

    if (Test-Path "Env:$Name") {
        return [string][Environment]::GetEnvironmentVariable($Name)
    }
    if ($EnvMap.ContainsKey($Name)) {
        return [string]$EnvMap[$Name]
    }
    return $Default
}

function Get-MergedIntConfigValue {
    param(
        [hashtable]$EnvMap,
        [string]$Name,
        [int]$Default
    )

    $raw = Get-MergedConfigValue -EnvMap $EnvMap -Name $Name -Default ([string]$Default)
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $Default
    }

    $parsed = 0
    if ([int]::TryParse($raw, [ref]$parsed)) {
        return $parsed
    }

    throw "Invalid integer value for ${Name}: $raw"
}

function Normalize-ExistingPath {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return ''
    }

    try {
        if (Test-Path -LiteralPath $Path) {
            return (Resolve-Path -LiteralPath $Path).Path
        }
    }
    catch {
    }

    return [System.IO.Path]::GetFullPath($Path)
}

function Ensure-ParentDirectory {
    param([string]$Path)

    $parent = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
}

function Read-IntWithDefault {
    param(
        [string]$Prompt,
        [int]$Default
    )

    while ($true) {
        $raw = Read-Host "$Prompt [$Default]"
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $Default
        }

        $parsed = 0
        if ([int]::TryParse($raw, [ref]$parsed) -and $parsed -gt 0) {
            return $parsed
        }

        Write-Host "Please enter an integer greater than 0." -ForegroundColor Yellow
    }
}

function Test-PortAvailable {
    param([int]$Port)

    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $listener) {
            try { $listener.Stop() } catch {}
        }
    }
}

function Test-PortListening {
    param(
        [string]$BindHost,
        [int]$Port,
        [int]$TimeoutMs = 400
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect($BindHost, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    }
    catch {
        return $false
    }
    finally {
        try { $client.Close() } catch {}
    }
}

function Wait-ForPort {
    param(
        [string]$BindHost,
        [int]$Port,
        [int]$TimeoutSeconds = 20
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortListening -BindHost $BindHost -Port $Port -TimeoutMs 500) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    }

    return $false
}

function Wait-ForProcessExit {
    param(
        [int]$ProcessId,
        [int]$TimeoutSeconds = 8
    )

    if ($ProcessId -le 0) {
        return $true
    }

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-ProcessAlive -ProcessId $ProcessId)) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    }

    return (-not (Test-ProcessAlive -ProcessId $ProcessId))
}

function Reset-LogFile {
    param(
        [string]$Path,
        [int]$TimeoutSeconds = 8
    )

    Ensure-ParentDirectory -Path $Path
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = ''
    while ((Get-Date) -lt $deadline) {
        try {
            [System.IO.File]::WriteAllText($Path, '', [System.Text.Encoding]::UTF8)
            return
        }
        catch {
            $lastError = $_.Exception.Message
            Start-Sleep -Milliseconds 250
        }
    }

    throw "Unable to reset log file ${Path}: $lastError"
}

function Get-FreePorts {
    param(
        [int]$Count,
        [int]$StartingPort
    )

    $ports = New-Object System.Collections.Generic.List[int]
    $port = $StartingPort
    while ($ports.Count -lt $Count) {
        if (Test-PortAvailable -Port $port) {
            $ports.Add($port)
        }
        $port += 1
    }
    return @($ports.ToArray())
}

function Resolve-CloudflaredExecutable {
    param([string]$PreferredCommand = '')

    if (-not [string]::IsNullOrWhiteSpace($PreferredCommand)) {
        if (Test-Path -LiteralPath $PreferredCommand) {
            return (Resolve-Path -LiteralPath $PreferredCommand).Path
        }

        $overrideCommand = Get-Command $PreferredCommand -ErrorAction SilentlyContinue
        if ($null -ne $overrideCommand) {
            return $overrideCommand.Source
        }
    }

    $command = Get-Command 'cloudflared' -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $localBinary = Join-Path $RepoRoot "tools\cloudflared.exe"
    if (Test-Path -LiteralPath $localBinary) {
        return (Resolve-Path -LiteralPath $localBinary).Path
    }

    throw "Missing required command: cloudflared. Install it on PATH or set NOTION_LOCAL_OPS_CLOUDFLARED_COMMAND."
}

function Resolve-ServerPaths {
    $pythonPath = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $entrypointPath = Join-Path $RepoRoot ".venv\Scripts\notion-local-ops-mcp.exe"

    return [pscustomobject]@{
        PythonPath = $pythonPath
        EntryPointPath = $entrypointPath
    }
}

function Test-LauncherRuntime {
    param([string]$RepoRoot)

    $paths = Resolve-ServerPaths
    $pythonPath = $paths.PythonPath
    $entrypointPath = $paths.EntryPointPath
    $repairCommand = '.\.venv\Scripts\python.exe -m pip install -e ".[dev]"'

    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw "Missing Python runtime: $pythonPath`nRun from repo root: $repairCommand"
    }

    if (-not (Test-Path -LiteralPath $entrypointPath)) {
        throw "Missing launcher entrypoint: $entrypointPath`nRun from repo root: $repairCommand"
    }

    $pyprojectPath = Join-Path $RepoRoot 'pyproject.toml'
    if (-not (Test-Path -LiteralPath $pyprojectPath)) {
        throw "Missing pyproject.toml: $pyprojectPath"
    }

    $pythonScript = @'
import json
import os
import pathlib
import re
import sys

repo_root = pathlib.Path(sys.argv[1])
pyproject_path = repo_root / "pyproject.toml"
payload = {
    "success": False,
    "supported_spec": "",
    "fastmcp_version": "",
    "uvicorn_version": "",
    "error": "",
}

try:
    text = pyproject_path.read_text(encoding="utf-8")
    match = re.search(r'["\']fastmcp([^"\']*)["\']', text)
    if match:
        payload["supported_spec"] = f"fastmcp{match.group(1)}"
except Exception as exc:  # pragma: no cover
    payload["error"] = f"failed to read pyproject.toml: {exc}"
    print(json.dumps(payload))
    raise SystemExit(0)

try:
    import fastmcp
    import uvicorn
except Exception as exc:
    payload["error"] = f"failed to import runtime dependencies: {exc}"
    print(json.dumps(payload))
    raise SystemExit(0)

fastmcp_version = os.environ.get("NOTION_LOCAL_OPS_TEST_FORCE_FASTMCP_VERSION") or getattr(fastmcp, "__version__", "")
uvicorn_version = getattr(uvicorn, "__version__", "")
payload["fastmcp_version"] = fastmcp_version
payload["uvicorn_version"] = uvicorn_version

min_major = None
max_major = None
for part in payload["supported_spec"].replace("fastmcp", "").split(","):
    token = part.strip()
    if token.startswith(">="):
        min_major = int(token[2:].split(".")[0])
    elif token.startswith("<"):
        max_major = int(token[1:].split(".")[0])

try:
    actual_major = int(fastmcp_version.split(".")[0])
except Exception:
    payload["error"] = f"unable to parse fastmcp version: {fastmcp_version!r}"
    print(json.dumps(payload))
    raise SystemExit(0)

if min_major is not None and actual_major < min_major:
    payload["error"] = f"fastmcp {fastmcp_version} is below supported range {payload['supported_spec']}"
elif max_major is not None and actual_major >= max_major:
    payload["error"] = f"fastmcp {fastmcp_version} is outside supported range {payload['supported_spec']}"
else:
    payload["success"] = True

print(json.dumps(payload))
'@

    $tempScript = [System.IO.Path]::GetTempFileName() + '.py'
    try {
        Set-Content -LiteralPath $tempScript -Value $pythonScript -Encoding UTF8
        $output = & $pythonPath $tempScript $RepoRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Runtime validation failed to execute.`nRun from repo root: $repairCommand"
        }
    }
    finally {
        if (Test-Path -LiteralPath $tempScript) {
            Remove-Item -LiteralPath $tempScript -Force -ErrorAction SilentlyContinue
        }
    }

    try {
        $result = $output | ConvertFrom-Json
    }
    catch {
        throw "Runtime validation returned invalid output: $output`nRun from repo root: $repairCommand"
    }

    if (-not $result.success) {
        $message = [string]$result.error
        if ([string]::IsNullOrWhiteSpace($message)) {
            $message = 'unknown runtime validation failure'
        }
        throw "$message`nRun from repo root: $repairCommand"
    }

    return [pscustomobject]@{
        PythonPath = $pythonPath
        EntryPointPath = $entrypointPath
        FastMcpVersion = [string]$result.fastmcp_version
        UvicornVersion = [string]$result.uvicorn_version
        SupportedSpec = [string]$result.supported_spec
    }
}

function Test-ProcessAlive {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return $false
    }

    return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Stop-ManagedProcess {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return
    }

    if (Test-ProcessAlive -ProcessId $ProcessId) {
        try {
            & taskkill.exe /PID $ProcessId /T /F | Out-Null
        }
        catch {
        }

        if (-not (Wait-ForProcessExit -ProcessId $ProcessId -TimeoutSeconds 8)) {
            try {
                Stop-Process -Id $ProcessId -Force -ErrorAction Stop
            }
            catch {
            }
            [void](Wait-ForProcessExit -ProcessId $ProcessId -TimeoutSeconds 2)
        }
    }
}

function Stop-ManagedInstances {
    param([object[]]$Instances)

    foreach ($instance in @($Instances)) {
        Stop-ManagedProcess -ProcessId ([int]$instance.CloudflaredProcessId)
        Stop-ManagedProcess -ProcessId ([int]$instance.ServerProcessId)
    }

    Start-Sleep -Milliseconds 500
}

function Get-OptionalPropertyValue {
    param(
        [object]$Object,
        [string]$Name,
        $Default = $null
    )

    if ($null -eq $Object) {
        return $Default
    }

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $Default
    }
    if ($null -eq $property.Value) {
        return $Default
    }
    return $property.Value
}

function Convert-StateInstance {
    param([object]$Item)

    $publicUrl = [string](Get-OptionalPropertyValue -Object $Item -Name 'public_url' -Default '')
    $publicMcpUrl = [string](Get-OptionalPropertyValue -Object $Item -Name 'public_mcp_url' -Default '')
    if ([string]::IsNullOrWhiteSpace($publicMcpUrl)) {
        $publicMcpUrl = [string](Get-OptionalPropertyValue -Object $Item -Name 'mcp_url' -Default '')
    }
    if ([string]::IsNullOrWhiteSpace($publicUrl) -and -not [string]::IsNullOrWhiteSpace($publicMcpUrl)) {
        $publicUrl = $publicMcpUrl -replace '/mcp/?$', ''
    }

    return [pscustomobject]@{
        Name = [string](Get-OptionalPropertyValue -Object $Item -Name 'name' -Default '')
        Host = [string](Get-OptionalPropertyValue -Object $Item -Name 'host' -Default '127.0.0.1')
        Port = [int](Get-OptionalPropertyValue -Object $Item -Name 'port' -Default 0)
        Token = [string](Get-OptionalPropertyValue -Object $Item -Name 'token' -Default '')
        LocalUrl = [string](Get-OptionalPropertyValue -Object $Item -Name 'local_url' -Default '')
        PublicUrl = $publicUrl
        PublicMcpUrl = $publicMcpUrl
        ServerProcessId = [int](Get-OptionalPropertyValue -Object $Item -Name 'server_pid' -Default 0)
        CloudflaredProcessId = [int](Get-OptionalPropertyValue -Object $Item -Name 'cloudflared_pid' -Default 0)
        StateDir = [string](Get-OptionalPropertyValue -Object $Item -Name 'state_dir' -Default '')
        InstanceDir = [string](Get-OptionalPropertyValue -Object $Item -Name 'instance_dir' -Default '')
        ServerLogPath = [string](Get-OptionalPropertyValue -Object $Item -Name 'server_log_path' -Default (Get-OptionalPropertyValue -Object $Item -Name 'server_stdout_log' -Default ''))
        CloudflaredStdoutLogPath = [string](Get-OptionalPropertyValue -Object $Item -Name 'cloudflared_stdout_log_path' -Default (Get-OptionalPropertyValue -Object $Item -Name 'cloudflared_stdout_log' -Default ''))
        CloudflaredStderrLogPath = [string](Get-OptionalPropertyValue -Object $Item -Name 'cloudflared_stderr_log_path' -Default (Get-OptionalPropertyValue -Object $Item -Name 'cloudflared_stderr_log' -Default ''))
        StartedAt = [string](Get-OptionalPropertyValue -Object $Item -Name 'started_at' -Default '')
        RestartCount = [int](Get-OptionalPropertyValue -Object $Item -Name 'restart_count' -Default 0)
        LastFailureReason = [string](Get-OptionalPropertyValue -Object $Item -Name 'last_failure_reason' -Default '')
        LastProbeAt = [string](Get-OptionalPropertyValue -Object $Item -Name 'last_probe_at' -Default '')
        TunnelMode = [string](Get-OptionalPropertyValue -Object $Item -Name 'tunnel_mode' -Default 'quick')
        ConsecutivePublicProbeFailures = [int](Get-OptionalPropertyValue -Object $Item -Name 'consecutive_public_probe_failures' -Default 0)
        ConsecutiveRepairFailures = [int](Get-OptionalPropertyValue -Object $Item -Name 'consecutive_repair_failures' -Default 0)
        NeedsNotionUrlUpdate = [bool](Get-OptionalPropertyValue -Object $Item -Name 'needs_notion_url_update' -Default $false)
        UrlChangedAt = [string](Get-OptionalPropertyValue -Object $Item -Name 'url_changed_at' -Default '')
        LastPublicProbeStatus = [string](Get-OptionalPropertyValue -Object $Item -Name 'last_public_probe_status' -Default 'unknown')
        LastPublicProbeMessage = [string](Get-OptionalPropertyValue -Object $Item -Name 'last_public_probe_message' -Default '')
        LastRepairAction = [string](Get-OptionalPropertyValue -Object $Item -Name 'last_repair_action' -Default '')
    }
}

function Read-LauncherState {
    param([string]$LauncherStatePath)

    if (-not (Test-Path -LiteralPath $LauncherStatePath)) {
        return $null
    }

    try {
        return Get-Content -LiteralPath $LauncherStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Remove-LauncherState {
    param([string]$LauncherStatePath)

    if (Test-Path -LiteralPath $LauncherStatePath) {
        Remove-Item -LiteralPath $LauncherStatePath -Force
    }
}

function Write-LauncherState {
    param(
        [string]$LauncherStatePath,
        [object[]]$Instances,
        [int]$RequestedCount,
        [int]$RequestedBasePort,
        [string]$BindHost,
        [string]$WorkspaceRoot
    )

    $payload = [ordered]@{
        launcher_version = 2
        started_at = (Get-Date).ToString('o')
        requested_count = $RequestedCount
        requested_base_port = $RequestedBasePort
        bind_host = $BindHost
        workspace_root = $WorkspaceRoot
        instances = @(
            @($Instances) | ForEach-Object {
                [ordered]@{
                    name = $_.Name
                    host = $_.Host
                    port = $_.Port
                    token = $_.Token
                    local_url = $_.LocalUrl
                    public_url = $_.PublicUrl
                    public_mcp_url = $_.PublicMcpUrl
                    mcp_url = $_.PublicMcpUrl
                    server_pid = $_.ServerProcessId
                    cloudflared_pid = $_.CloudflaredProcessId
                    state_dir = $_.StateDir
                    instance_dir = $_.InstanceDir
                    server_log_path = $_.ServerLogPath
                    cloudflared_stdout_log_path = $_.CloudflaredStdoutLogPath
                    cloudflared_stderr_log_path = $_.CloudflaredStderrLogPath
                    started_at = $_.StartedAt
                    restart_count = $_.RestartCount
                    last_failure_reason = $_.LastFailureReason
                    last_probe_at = $_.LastProbeAt
                    tunnel_mode = $_.TunnelMode
                    consecutive_public_probe_failures = $_.ConsecutivePublicProbeFailures
                    consecutive_repair_failures = $_.ConsecutiveRepairFailures
                    needs_notion_url_update = $_.NeedsNotionUrlUpdate
                    url_changed_at = $_.UrlChangedAt
                    last_public_probe_status = $_.LastPublicProbeStatus
                    last_public_probe_message = $_.LastPublicProbeMessage
                    last_repair_action = $_.LastRepairAction
                }
            }
        )
    }

    Ensure-ParentDirectory -Path $LauncherStatePath
    $payload | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $LauncherStatePath -Encoding UTF8
}

function Convert-LauncherStateToInstances {
    param([object]$State)

    if ($null -eq $State) {
        return @()
    }

    return @(@($State.instances) | ForEach-Object { Convert-StateInstance -Item $_ })
}

function Test-InstanceHealthyForReuse {
    param([pscustomobject]$Instance)

    if (-not (Test-ProcessAlive -ProcessId ([int]$Instance.ServerProcessId))) {
        return $false
    }
    if (-not (Test-ProcessAlive -ProcessId ([int]$Instance.CloudflaredProcessId))) {
        return $false
    }
    if (-not (Test-PortListening -BindHost $Instance.Host -Port $Instance.Port -TimeoutMs 500)) {
        return $false
    }
    if ([string]::IsNullOrWhiteSpace($Instance.PublicUrl) -or [string]::IsNullOrWhiteSpace($Instance.PublicMcpUrl)) {
        return $false
    }

    return $true
}

function Test-LauncherStateHealthy {
    param([object]$State)

    $instances = Convert-LauncherStateToInstances -State $State
    if ($instances.Count -eq 0) {
        return $false
    }

    foreach ($instance in $instances) {
        if (-not (Test-InstanceHealthyForReuse -Instance $instance)) {
            return $false
        }
    }

    return $true
}

function Test-LauncherStateMatchesRequest {
    param(
        [object]$State,
        [int]$RequestedCount,
        [int]$RequestedBasePort,
        [string]$BindHost,
        [string]$WorkspaceRoot
    )

    if ($null -eq $State) {
        return $false
    }

    $stateCount = [int](Get-OptionalPropertyValue -Object $State -Name 'requested_count' -Default (@($State.instances).Count))
    $stateBasePort = [int](Get-OptionalPropertyValue -Object $State -Name 'requested_base_port' -Default ([int](Get-OptionalPropertyValue -Object $State.instances[0] -Name 'port' -Default 0)))
    $stateBindHost = [string](Get-OptionalPropertyValue -Object $State -Name 'bind_host' -Default '')
    $stateWorkspaceRoot = [string](Get-OptionalPropertyValue -Object $State -Name 'workspace_root' -Default '')

    if ($stateCount -ne $RequestedCount) {
        return $false
    }
    if ($stateBasePort -ne $RequestedBasePort) {
        return $false
    }
    if ($stateBindHost -ne $BindHost) {
        return $false
    }
    if ((Normalize-ExistingPath -Path $stateWorkspaceRoot) -ne (Normalize-ExistingPath -Path $WorkspaceRoot)) {
        return $false
    }

    return $true
}

function Wait-ForQuickTunnelUrl {
    param(
        [string[]]$LogPaths,
        [int]$TimeoutSeconds = 45,
        [int]$ProcessId = 0
    )

    $quickTunnelPattern = 'https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com'
    $localTestPattern = 'https?://(?:127\.0\.0\.1|localhost):\d+'
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $combined = ''
        foreach ($logPath in @($LogPaths)) {
            if (-not [string]::IsNullOrWhiteSpace($logPath) -and (Test-Path -LiteralPath $logPath)) {
                try {
                    $combined += "`n" + (Get-Content -LiteralPath $logPath -Raw -Encoding UTF8 -ErrorAction Stop)
                }
                catch {
                }
            }
        }

        if (-not [string]::IsNullOrWhiteSpace($combined)) {
            $quickMatches = [regex]::Matches($combined, $quickTunnelPattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
            if ($quickMatches.Count -gt 0) {
                return $quickMatches[$quickMatches.Count - 1].Value
            }

            $localMatches = [regex]::Matches($combined, $localTestPattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
            if ($localMatches.Count -gt 0) {
                return $localMatches[$localMatches.Count - 1].Value
            }
        }

        if ($ProcessId -gt 0 -and -not (Test-ProcessAlive -ProcessId $ProcessId)) {
            if ($combined -match 'failed to request quick Tunnel') {
                break
            }
        }

        Start-Sleep -Milliseconds 500
    }

    return $null
}

function Get-RequestedInstanceToken {
    param(
        [int]$Index,
        [string]$PrimaryToken,
        [string]$SecondToken
    )

    if ($Index -eq 2 -and -not [string]::IsNullOrWhiteSpace($SecondToken)) {
        return $SecondToken
    }

    return $PrimaryToken
}

function Initialize-InstanceLayout {
    param([pscustomobject]$Instance)

    New-Item -ItemType Directory -Force -Path $Instance.StateDir | Out-Null
    New-Item -ItemType Directory -Force -Path $Instance.InstanceDir | Out-Null

    foreach ($path in @($Instance.ServerLogPath, $Instance.CloudflaredStdoutLogPath, $Instance.CloudflaredStderrLogPath)) {
        if (-not (Test-Path -LiteralPath $path)) {
            New-Item -ItemType File -Force -Path $path | Out-Null
        }
    }
}

function New-ManagedInstanceRecord {
    param(
        [int]$Index,
        [int]$Port,
        [string]$BindHost,
        [string]$WorkspaceRoot,
        [string]$BaseStateDir,
        [string]$AuthToken,
        [string]$LauncherInstancesDir
    )

    $instanceName = "mcp-$Index"
    $instanceStateDir = Join-Path $BaseStateDir ("instance-{0}-port-{1}" -f $Index, $Port)
    $instanceDir = Join-Path $LauncherInstancesDir ("{0}-port-{1}" -f $instanceName, $Port)
    $serverLogPath = Join-Path $instanceDir 'server.log'
    $cloudflaredStdoutLogPath = Join-Path $instanceDir 'cloudflared.stdout.log'
    $cloudflaredStderrLogPath = Join-Path $instanceDir 'cloudflared.stderr.log'
    $originUrl = "http://$BindHost`:$Port"

    $instance = [pscustomobject]@{
        Name = $instanceName
        Host = $BindHost
        Port = $Port
        Token = $AuthToken
        LocalUrl = "$originUrl/mcp"
        PublicUrl = ''
        PublicMcpUrl = ''
        ServerProcessId = 0
        CloudflaredProcessId = 0
        StateDir = $instanceStateDir
        InstanceDir = $instanceDir
        ServerLogPath = $serverLogPath
        CloudflaredStdoutLogPath = $cloudflaredStdoutLogPath
        CloudflaredStderrLogPath = $cloudflaredStderrLogPath
        StartedAt = ''
        RestartCount = 0
        LastFailureReason = ''
        LastProbeAt = ''
        TunnelMode = 'quick'
        ConsecutivePublicProbeFailures = 0
        ConsecutiveRepairFailures = 0
        NeedsNotionUrlUpdate = $false
        UrlChangedAt = ''
        LastPublicProbeStatus = 'unknown'
        LastPublicProbeMessage = ''
        LastRepairAction = ''
    }

    Initialize-InstanceLayout -Instance $instance
    return $instance
}

function Start-ManagedServer {
    param(
        [pscustomobject]$Instance,
        [pscustomobject]$RuntimeConfig
    )

    $runnerScript = Join-Path $PSScriptRoot 'run-mcp-instance.ps1'
    if (-not (Test-Path -LiteralPath $runnerScript)) {
        throw "Missing runner script: $runnerScript"
    }

    $argList = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy Bypass',
        ('-File "{0}"' -f $runnerScript),
        ('-ServerExecutable "{0}"' -f $RuntimeConfig.ServerExecutable),
        ('-BindHost "{0}"' -f $Instance.Host),
        ('-Port {0}' -f $Instance.Port),
        ('-WorkspaceRoot "{0}"' -f $RuntimeConfig.WorkspaceRoot),
        ('-StateDir "{0}"' -f $Instance.StateDir),
        ('-AuthToken "{0}"' -f $Instance.Token),
        ('-CodexCommand "{0}"' -f $RuntimeConfig.CodexCommand),
        ('-ClaudeCommand "{0}"' -f $RuntimeConfig.ClaudeCommand),
        ('-CommandTimeout {0}' -f $RuntimeConfig.CommandTimeout),
        ('-DelegateTimeout {0}' -f $RuntimeConfig.DelegateTimeout),
        ('-LogPath "{0}"' -f $Instance.ServerLogPath)
    ) -join ' '

    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
    if (-not (Wait-ForPort -BindHost $Instance.Host -Port $Instance.Port -TimeoutSeconds 20)) {
        Stop-ManagedProcess -ProcessId $process.Id
        throw "MCP instance $($Instance.Name) did not become ready on http://$($Instance.Host):$($Instance.Port). See $($Instance.ServerLogPath)"
    }

    $Instance.ServerProcessId = $process.Id
    $Instance.StartedAt = (Get-Date).ToString('o')
}

function Start-QuickTunnel {
    param(
        [pscustomobject]$Instance,
        [pscustomobject]$RuntimeConfig
    )

    Stop-ManagedProcess -ProcessId ([int]$Instance.CloudflaredProcessId)
    foreach ($path in @($Instance.CloudflaredStdoutLogPath, $Instance.CloudflaredStderrLogPath)) {
        Reset-LogFile -Path $path
    }

    $originUrl = "http://$($Instance.Host):$($Instance.Port)"
    $quickTunnelUrl = $null
    $launchAttempts = 3
    $process = $null
    $cloudflaredExecutable = $RuntimeConfig.CloudflaredExecutable
    for ($attempt = 1; $attempt -le $launchAttempts; $attempt++) {
        $extension = [System.IO.Path]::GetExtension($cloudflaredExecutable).ToLowerInvariant()
        if ($extension -eq '.ps1') {
            $process = Start-Process `
                -FilePath 'powershell.exe' `
                -ArgumentList @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $cloudflaredExecutable, 'tunnel', '--url', $originUrl) `
                -WorkingDirectory $RepoRoot `
                -RedirectStandardOutput $Instance.CloudflaredStdoutLogPath `
                -RedirectStandardError $Instance.CloudflaredStderrLogPath `
                -WindowStyle Hidden `
                -PassThru
        }
        elseif ($extension -eq '.py') {
            $process = Start-Process `
                -FilePath $RuntimeConfig.PythonPath `
                -ArgumentList @($cloudflaredExecutable, 'tunnel', '--url', $originUrl) `
                -WorkingDirectory $RepoRoot `
                -RedirectStandardOutput $Instance.CloudflaredStdoutLogPath `
                -RedirectStandardError $Instance.CloudflaredStderrLogPath `
                -WindowStyle Hidden `
                -PassThru
        }
        else {
            $process = Start-Process `
                -FilePath $cloudflaredExecutable `
                -ArgumentList @('tunnel', '--url', $originUrl) `
                -WorkingDirectory $RepoRoot `
                -RedirectStandardOutput $Instance.CloudflaredStdoutLogPath `
                -RedirectStandardError $Instance.CloudflaredStderrLogPath `
                -WindowStyle Hidden `
                -PassThru
        }

        $quickTunnelUrl = Wait-ForQuickTunnelUrl -LogPaths @($Instance.CloudflaredStderrLogPath, $Instance.CloudflaredStdoutLogPath) -TimeoutSeconds 25 -ProcessId $process.Id
        if (-not [string]::IsNullOrWhiteSpace($quickTunnelUrl)) {
            break
        }

            Stop-ManagedProcess -ProcessId $process.Id
            $process = $null
            if ($attempt -lt $launchAttempts) {
                Start-Sleep -Seconds 2
            }
    }

    if ([string]::IsNullOrWhiteSpace($quickTunnelUrl) -or $null -eq $process) {
        throw "Failed to extract quick tunnel URL for $($Instance.Name) after $launchAttempts attempts. See $($Instance.CloudflaredStderrLogPath)"
    }

    $oldPublicMcpUrl = [string]$Instance.PublicMcpUrl
    $Instance.CloudflaredProcessId = $process.Id
    $Instance.PublicUrl = $quickTunnelUrl
    $Instance.PublicMcpUrl = "$quickTunnelUrl/mcp"
    $Instance.TunnelMode = 'quick'
    if (-not [string]::IsNullOrWhiteSpace($oldPublicMcpUrl) -and $oldPublicMcpUrl -ne $Instance.PublicMcpUrl) {
        $Instance.NeedsNotionUrlUpdate = $true
        $Instance.UrlChangedAt = (Get-Date).ToString('o')
    }
}

function Invoke-PublicMcpProbe {
    param(
        [pscustomobject]$Instance,
        [int]$TimeoutSeconds = 5
    )

    if ([string]::IsNullOrWhiteSpace($Instance.PublicMcpUrl)) {
        return [pscustomobject]@{
            Success = $false
            StatusCode = 0
            Message = 'missing public MCP URL'
        }
    }

    $headers = @{}
    if (-not [string]::IsNullOrWhiteSpace($Instance.Token)) {
        $headers['Authorization'] = "Bearer $($Instance.Token)"
    }

    try {
        $response = Invoke-WebRequest -Uri $Instance.PublicMcpUrl -Method Head -Headers $headers -UseBasicParsing -TimeoutSec $TimeoutSeconds
        $statusCode = [int]$response.StatusCode
        return [pscustomobject]@{
            Success = ($statusCode -eq 200 -or $statusCode -eq 204)
            StatusCode = $statusCode
            Message = "HTTP $statusCode"
        }
    }
    catch {
        $statusCode = 0
        $message = $_.Exception.Message
        $response = $_.Exception.Response
        if ($null -ne $response) {
            try {
                $statusCode = [int]$response.StatusCode.value__
                $message = "HTTP $statusCode"
            }
            catch {
            }
        }
        return [pscustomobject]@{
            Success = $false
            StatusCode = $statusCode
            Message = $message
        }
    }
}

function Probe-LocalInstance {
    param([pscustomobject]$Instance)

    $serverAlive = Test-ProcessAlive -ProcessId ([int]$Instance.ServerProcessId)
    $listening = Test-PortListening -BindHost $Instance.Host -Port $Instance.Port

    $message = if ($serverAlive -and $listening) {
        'process alive and port listening'
    }
    elseif ($serverAlive -and -not $listening) {
        'process alive but port not listening'
    }
    elseif (-not $serverAlive -and $listening) {
        'port listening but managed server PID missing'
    }
    else {
        'process stopped and port closed'
    }

    return [pscustomobject]@{
        Success = ($serverAlive -and $listening)
        Message = $message
        ServerAlive = $serverAlive
        Listening = $listening
    }
}

function Get-RestartBackoffSeconds {
    param([int]$FailureCount)

    switch ([Math]::Min([Math]::Max($FailureCount, 0), 2)) {
        0 { return 2 }
        1 { return 5 }
        default { return 10 }
    }
}

function Repair-Instance {
    param(
        [pscustomobject]$Instance,
        [pscustomobject]$RuntimeConfig,
        [bool]$RestartServer,
        [bool]$RestartTunnel,
        [string]$Reason
    )

    $backoffSeconds = Get-RestartBackoffSeconds -FailureCount ([int]$Instance.ConsecutiveRepairFailures)
    if ($backoffSeconds -gt 0) {
        Start-Sleep -Seconds $backoffSeconds
    }

    $Instance.RestartCount = [int]$Instance.RestartCount + 1
    $Instance.ConsecutiveRepairFailures = [int]$Instance.ConsecutiveRepairFailures + 1
    $Instance.LastFailureReason = $Reason
    if ($RestartServer) {
        $Instance.LastRepairAction = 'restart_server_and_tunnel'
    }
    else {
        $Instance.LastRepairAction = 'restart_tunnel'
    }

    try {
        if ($RestartTunnel) {
            Stop-ManagedProcess -ProcessId ([int]$Instance.CloudflaredProcessId)
            $Instance.CloudflaredProcessId = 0
        }
        if ($RestartServer) {
            Stop-ManagedProcess -ProcessId ([int]$Instance.ServerProcessId)
            $Instance.ServerProcessId = 0
        }

        if ($RestartServer) {
            Start-ManagedServer -Instance $Instance -RuntimeConfig $RuntimeConfig
            Start-QuickTunnel -Instance $Instance -RuntimeConfig $RuntimeConfig
        }
        elseif ($RestartTunnel) {
            Start-QuickTunnel -Instance $Instance -RuntimeConfig $RuntimeConfig
        }

        $Instance.ConsecutivePublicProbeFailures = 0
        $Instance.ConsecutiveRepairFailures = 0
        $Instance.LastPublicProbeStatus = 'pending'
        $Instance.LastPublicProbeMessage = 'waiting for next probe after repair'
        $Instance.LastFailureReason = ''
        return $true
    }
    catch {
        $Instance.LastFailureReason = "$Reason :: $($_.Exception.Message)"
        return $false
    }
}

function Get-InstanceStatus {
    param([pscustomobject]$Instance)

    $serverAlive = Test-ProcessAlive -ProcessId ([int]$Instance.ServerProcessId)
    $tunnelAlive = Test-ProcessAlive -ProcessId ([int]$Instance.CloudflaredProcessId)
    $listening = Test-PortListening -BindHost $Instance.Host -Port $Instance.Port

    $status = if ($serverAlive -and $listening -and $Instance.LastPublicProbeStatus -eq 'ok') {
        'Running'
    }
    elseif ($serverAlive -and $listening -and $Instance.LastPublicProbeStatus -eq 'failed') {
        'PublicProbeFail'
    }
    elseif ($serverAlive -and $listening -and -not $tunnelAlive) {
        'TunnelStopped'
    }
    elseif ($serverAlive -and -not $listening) {
        'Starting'
    }
    elseif (-not $serverAlive -and $tunnelAlive) {
        'ServerStopped'
    }
    else {
        'Stopped'
    }

    return [pscustomobject]@{
        Instance = $Instance.Name
        ServerPID = $Instance.ServerProcessId
        TunnelPID = $Instance.CloudflaredProcessId
        Port = $Instance.Port
        Status = $status
        Token = $Instance.Token
        LocalUrl = $Instance.LocalUrl
        PublicUrl = $Instance.PublicUrl
        PublicMcpUrl = $Instance.PublicMcpUrl
        RestartCount = $Instance.RestartCount
        LastProbeAt = $Instance.LastProbeAt
        LastFailureReason = $Instance.LastFailureReason
        NeedsNotionUrlUpdate = $Instance.NeedsNotionUrlUpdate
        ServerLog = $Instance.ServerLogPath
        CloudflaredStdoutLog = $Instance.CloudflaredStdoutLogPath
        CloudflaredStderrLog = $Instance.CloudflaredStderrLogPath
    }
}

function Write-StoppedSnapshot {
    param(
        [string]$StatusPath,
        [string]$WorkspaceRoot,
        [string]$LauncherStatePath
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add(("Updated: {0}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')))
    $lines.Add(("Repo: {0}" -f $RepoRoot))
    $lines.Add(("Workspace: {0}" -f $WorkspaceRoot))
    $lines.Add('')
    $lines.Add('All managed MCP instances and Cloudflare quick tunnels have been stopped.')
    $lines.Add(("State File: {0}" -f $LauncherStatePath))
    Ensure-ParentDirectory -Path $StatusPath
    Set-Content -LiteralPath $StatusPath -Value $lines -Encoding UTF8
}

function Write-StatusSnapshot {
    param(
        [string]$StatusPath,
        [object[]]$Rows,
        [int]$RequestedCount,
        [int]$RequestedBasePort,
        [string]$BindHost,
        [string]$WorkspaceRoot,
        [string]$LauncherStatePath
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add(("Updated: {0}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')))
    $lines.Add(("Repo: {0}" -f $RepoRoot))
    $lines.Add(("Workspace: {0}" -f $WorkspaceRoot))
    $lines.Add(("Bind Host: {0}" -f $BindHost))
    $lines.Add(("Requested Count: {0}" -f $RequestedCount))
    $lines.Add(("Requested Base Port: {0}" -f $RequestedBasePort))
    $lines.Add(("State File: {0}" -f $LauncherStatePath))
    $lines.Add('')

    foreach ($row in $Rows) {
        $lines.Add(("[{0}] {1}" -f $row.Instance, $row.Status))
        $lines.Add(("  Token: {0}" -f $row.Token))
        $lines.Add(("  Local URL: {0}" -f $row.LocalUrl))
        $lines.Add(("  Public URL: {0}" -f $row.PublicUrl))
        $lines.Add(("  Public MCP URL: {0}" -f $row.PublicMcpUrl))
        $lines.Add(("  Restart Count: {0}" -f $row.RestartCount))
        $lines.Add(("  Last Probe At: {0}" -f $row.LastProbeAt))
        $lines.Add(("  Server PID: {0}" -f $row.ServerPID))
        $lines.Add(("  cloudflared PID: {0}" -f $row.TunnelPID))
        if ($row.NeedsNotionUrlUpdate) {
            $lines.Add('  NOTE: Public MCP URL changed. Update the Notion connector URL manually.')
        }
        if (-not [string]::IsNullOrWhiteSpace($row.LastFailureReason)) {
            $lines.Add(("  Last Failure: {0}" -f $row.LastFailureReason))
        }
        $lines.Add(("  Server Log: {0}" -f $row.ServerLog))
        $lines.Add(("  Tunnel stdout log: {0}" -f $row.CloudflaredStdoutLog))
        $lines.Add(("  Tunnel stderr log: {0}" -f $row.CloudflaredStderrLog))
        $lines.Add('')
    }

    Ensure-ParentDirectory -Path $StatusPath
    Set-Content -LiteralPath $StatusPath -Value $lines -Encoding UTF8
}

function Try-ReadPanelCommand {
    if ($NonInteractive) {
        return ''
    }

    try {
        if (-not [Console]::KeyAvailable) {
            return ''
        }

        $key = [Console]::ReadKey($true)
        switch ($key.Key) {
            'Enter' { return 'refresh' }
            'R' { return 'refresh' }
            'L' { return 'logs' }
            'S' { return 'stop' }
            'Q' { return 'quit' }
            default { return "unknown:$($key.KeyChar)" }
        }
    }
    catch {
        return ''
    }
}

function Handle-PanelCommand {
    param(
        [string]$Command,
        [object[]]$Instances,
        [string]$LauncherStateDir,
        [string]$LauncherStatePath,
        [string]$WorkspaceRoot,
        [string]$StatusPath
    )

    switch ($Command) {
        '' { return 'continue' }
        'refresh' { return 'continue' }
        'logs' {
            Start-Process explorer.exe $LauncherStateDir | Out-Null
            return 'continue'
        }
        'stop' {
            Write-Host ''
            Write-Host 'Stopping all managed MCP instances and Cloudflare quick tunnels...' -ForegroundColor Yellow
            Stop-ManagedInstances -Instances $Instances
            Remove-LauncherState -LauncherStatePath $LauncherStatePath
            Write-StoppedSnapshot -StatusPath $StatusPath -WorkspaceRoot $WorkspaceRoot -LauncherStatePath $LauncherStatePath
            Write-Host 'All managed MCP instances and tunnels have been stopped.' -ForegroundColor Green
            return 'stop'
        }
        'quit' {
            return 'quit'
        }
        default {
            if (-not $NonInteractive) {
                Write-Host ("Unknown command key: {0}" -f $Command) -ForegroundColor Red
                Write-Host 'Valid keys: [Enter]/R refresh, L logs, S stop, Q quit' -ForegroundColor Yellow
                Start-Sleep -Seconds 1
            }
            return 'continue'
        }
    }
}

function Render-StatusPanel {
    param(
        [object[]]$Rows,
        [string]$WorkspaceRoot,
        [string]$StatusPath,
        [string]$LauncherStatePath
    )

    if ($NonInteractive) {
        return
    }

    Clear-Host
    Write-Host '=== Notion Local MCP Status ===' -ForegroundColor Cyan
    Write-Host ("Repo: {0}" -f $RepoRoot)
    Write-Host ("Workspace: {0}" -f $WorkspaceRoot)
    Write-Host ("Desktop status file: {0}" -f $StatusPath)
    Write-Host ("State file: {0}" -f $LauncherStatePath)
    Write-Host ''

    $Rows | Format-Table -AutoSize Instance,ServerPID,TunnelPID,Port,Status,RestartCount,PublicMcpUrl

    Write-Host ''
    Write-Host 'Details:' -ForegroundColor DarkCyan
    foreach ($row in $Rows) {
        Write-Host ("- {0}" -f $row.Instance) -ForegroundColor DarkCyan
        Write-Host ("    Local URL: {0}" -f $row.LocalUrl)
        Write-Host ("    Public URL: {0}" -f $row.PublicUrl)
        Write-Host ("    Public MCP URL: {0}" -f $row.PublicMcpUrl)
        Write-Host ("    Restart Count: {0}" -f $row.RestartCount)
        if ($row.NeedsNotionUrlUpdate) {
            Write-Host '    NOTE: Public MCP URL changed. Update Notion manually.' -ForegroundColor Yellow
        }
        if (-not [string]::IsNullOrWhiteSpace($row.LastFailureReason)) {
            Write-Host ("    Last Failure: {0}" -f $row.LastFailureReason) -ForegroundColor Yellow
        }
        Write-Host ("    Server Log: {0}" -f $row.ServerLog)
        Write-Host ("    Tunnel stdout log: {0}" -f $row.CloudflaredStdoutLog)
        Write-Host ("    Tunnel stderr log: {0}" -f $row.CloudflaredStderrLog)
    }
    Write-Host ''
    Write-Host 'Keys: [Enter]/R=refresh  L=open logs  S=shutdown all  Q=close panel only' -ForegroundColor Yellow
}

function Monitor-ManagedInstance {
    param(
        [pscustomobject]$Instance,
        [pscustomobject]$RuntimeConfig
    )

    $Instance.LastProbeAt = (Get-Date).ToString('o')
    $localProbe = Probe-LocalInstance -Instance $Instance

    if ($localProbe.Success) {
        $publicProbeTimeoutSeconds = 5
        if ($null -ne $RuntimeConfig -and $null -ne $RuntimeConfig.PSObject.Properties['PublicProbeTimeoutSeconds']) {
            $publicProbeTimeoutSeconds = [int]$RuntimeConfig.PublicProbeTimeoutSeconds
        }
        $publicProbe = Invoke-PublicMcpProbe -Instance $Instance -TimeoutSeconds $publicProbeTimeoutSeconds
        if ($publicProbe.Success) {
            $Instance.ConsecutivePublicProbeFailures = 0
            $Instance.ConsecutiveRepairFailures = 0
            $Instance.LastPublicProbeStatus = 'ok'
            $Instance.LastPublicProbeMessage = $publicProbe.Message
            if ([string]::IsNullOrWhiteSpace($Instance.LastFailureReason)) {
                $Instance.LastFailureReason = ''
            }
        }
        else {
            $Instance.ConsecutivePublicProbeFailures = [int]$Instance.ConsecutivePublicProbeFailures + 1
            $Instance.LastPublicProbeStatus = 'failed'
            $Instance.LastPublicProbeMessage = $publicProbe.Message
            $Instance.LastFailureReason = "Public MCP probe failed ($($Instance.ConsecutivePublicProbeFailures)/3): $($publicProbe.Message)"
            if ($Instance.ConsecutivePublicProbeFailures -ge 3) {
                [void](Repair-Instance `
                    -Instance $Instance `
                    -RuntimeConfig $RuntimeConfig `
                    -RestartServer $false `
                    -RestartTunnel $true `
                    -Reason "Public MCP probe failed 3 times: $($publicProbe.Message)")
            }
        }
    }
    else {
        $Instance.ConsecutivePublicProbeFailures = 0
        $Instance.LastPublicProbeStatus = 'skipped'
        $Instance.LastPublicProbeMessage = "skipped: $($localProbe.Message)"
        $Instance.LastFailureReason = "Local instance unhealthy: $($localProbe.Message)"
        [void](Repair-Instance `
            -Instance $Instance `
            -RuntimeConfig $RuntimeConfig `
            -RestartServer $true `
            -RestartTunnel $true `
            -Reason $Instance.LastFailureReason)
    }

    return Get-InstanceStatus -Instance $Instance
}

$envMap = Read-DotEnvFile -Path (Join-Path $RepoRoot '.env')
$launcherStateDir = Get-MergedConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_LAUNCHER_STATE_DIR' -Default (Join-Path $RepoRoot '.state\launcher')
$launcherStateDir = Normalize-ExistingPath -Path $launcherStateDir
$launcherInstancesDir = Join-Path $launcherStateDir 'instances'
$launcherStatePath = Join-Path $launcherStateDir 'active-instances.json'

New-Item -ItemType Directory -Force -Path $launcherStateDir | Out-Null
New-Item -ItemType Directory -Force -Path $launcherInstancesDir | Out-Null

$statusPath = Get-MergedConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_STATUS_PATH' -Default $DesktopStatusPath
$statusPath = Normalize-ExistingPath -Path $statusPath
$bindHost = Get-MergedConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_HOST' -Default '127.0.0.1'
$basePort = Get-MergedIntConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_PORT' -Default $DefaultBasePort
$workspaceRoot = Get-MergedConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_WORKSPACE_ROOT' -Default $RepoRoot
$workspaceRoot = Normalize-ExistingPath -Path $workspaceRoot
$baseStateDir = Get-MergedConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_STATE_DIR' -Default (Join-Path $RepoRoot '.state\mcp-instances')
$baseStateDir = Normalize-ExistingPath -Path $baseStateDir
$authToken = Get-MergedConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_AUTH_TOKEN' -Default ''
$secondAuthToken = Get-MergedConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_AUTH_TOKEN_SECOND' -Default $authToken
$cloudflaredCommand = Get-MergedConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_CLOUDFLARED_COMMAND' -Default ''
$codexCommand = Get-MergedConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_CODEX_COMMAND' -Default 'codex'
$claudeCommand = Get-MergedConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_CLAUDE_COMMAND' -Default 'claude'
$commandTimeout = Get-MergedIntConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_COMMAND_TIMEOUT' -Default 120
$delegateTimeout = Get-MergedIntConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_DELEGATE_TIMEOUT' -Default 1800
$publicProbeTimeoutSeconds = Get-MergedIntConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_TEST_PUBLIC_PROBE_TIMEOUT_SECONDS' -Default 5

if ($MonitorIntervalSeconds -le 0) {
    throw "MonitorIntervalSeconds must be greater than 0."
}

$runtimeValidation = Test-LauncherRuntime -RepoRoot $RepoRoot
$serverExecutable = $runtimeValidation.EntryPointPath
$cloudflaredExecutable = Resolve-CloudflaredExecutable -PreferredCommand $cloudflaredCommand

$runtimeConfig = [pscustomobject]@{
    ServerExecutable = $serverExecutable
    PythonPath = $runtimeValidation.PythonPath
    CloudflaredExecutable = $cloudflaredExecutable
    WorkspaceRoot = $workspaceRoot
    CodexCommand = $codexCommand
    ClaudeCommand = $claudeCommand
    CommandTimeout = $commandTimeout
    DelegateTimeout = $delegateTimeout
    PublicProbeTimeoutSeconds = $publicProbeTimeoutSeconds
}

if ($NonInteractive) {
    $count = if ($RequestedCount -gt 0) { $RequestedCount } else { $DefaultCount }
    $requestedBasePort = if ($RequestedBasePort -gt 0) { $RequestedBasePort } else { $basePort }
}
else {
    Clear-Host
    Write-Host '=== Notion Local MCP Launcher ===' -ForegroundColor Cyan
    Write-Host "Repo: $RepoRoot"
    Write-Host "Workspace: $workspaceRoot"
    Write-Host "Base port: $basePort"
    Write-Host "fastmcp: $($runtimeValidation.FastMcpVersion) (supported: $($runtimeValidation.SupportedSpec))"
    Write-Host "uvicorn: $($runtimeValidation.UvicornVersion)"
    Write-Host "Cloudflared: $cloudflaredExecutable"
    Write-Host ''

    if ($RequestedCount -gt 0) {
        $count = $RequestedCount
    }
    else {
        $count = Read-IntWithDefault -Prompt 'How many MCP instances?' -Default $DefaultCount
    }

    if ($RequestedBasePort -gt 0) {
        $requestedBasePort = $RequestedBasePort
    }
    else {
        $requestedBasePort = Read-IntWithDefault -Prompt 'Starting port?' -Default $basePort
    }
}

if (-not (Test-Path -LiteralPath $workspaceRoot)) {
    throw "Workspace does not exist: $workspaceRoot"
}

$instances = @()
$existingState = Read-LauncherState -LauncherStatePath $launcherStatePath
if ($null -ne $existingState) {
    $stateInstances = Convert-LauncherStateToInstances -State $existingState
    $stateHealthy = Test-LauncherStateHealthy -State $existingState
    $stateMatches = $false
    if ($stateHealthy) {
        $stateMatches = Test-LauncherStateMatchesRequest `
            -State $existingState `
            -RequestedCount $count `
            -RequestedBasePort $requestedBasePort `
            -BindHost $bindHost `
            -WorkspaceRoot $workspaceRoot
    }

    if ($stateHealthy -and $stateMatches) {
        Write-Host 'Reusing existing MCP instances and quick tunnels.' -ForegroundColor Green
        $instances = $stateInstances
    }
    else {
        if ($stateHealthy) {
            Write-Host 'Existing launcher state does not match the requested configuration. Restarting managed instances...' -ForegroundColor Yellow
        }
        else {
            Write-Host 'Found stale launcher state. Cleaning up stale processes...' -ForegroundColor Yellow
        }
        Stop-ManagedInstances -Instances $stateInstances
        Remove-LauncherState -LauncherStatePath $launcherStatePath
    }
}

if ($instances.Count -eq 0) {
    $ports = @(Get-FreePorts -Count $count -StartingPort $requestedBasePort)
    $startedInstances = New-Object System.Collections.Generic.List[object]

    try {
        for ($i = 0; $i -lt $ports.Count; $i++) {
            $token = Get-RequestedInstanceToken -Index ($i + 1) -PrimaryToken $authToken -SecondToken $secondAuthToken
            $instance = New-ManagedInstanceRecord `
                -Index ($i + 1) `
                -Port $ports[$i] `
                -BindHost $bindHost `
                -WorkspaceRoot $workspaceRoot `
                -BaseStateDir $baseStateDir `
                -AuthToken $token `
                -LauncherInstancesDir $launcherInstancesDir

            Start-ManagedServer -Instance $instance -RuntimeConfig $runtimeConfig
            Start-QuickTunnel -Instance $instance -RuntimeConfig $runtimeConfig
            [void]$startedInstances.Add($instance)
        }

        $instances = @($startedInstances.ToArray())
    }
    catch {
        Stop-ManagedInstances -Instances @($startedInstances.ToArray())
        Remove-LauncherState -LauncherStatePath $launcherStatePath
        throw
    }
}

Write-Host ''
Write-Host 'Instances ready. Monitoring local health + public MCP probe...' -ForegroundColor Green
Start-Sleep -Seconds 1

$cycle = 0
while ($true) {
    $cycle += 1

    $rows = @($instances | ForEach-Object { Monitor-ManagedInstance -Instance $_ -RuntimeConfig $runtimeConfig })
    Write-LauncherState `
        -LauncherStatePath $launcherStatePath `
        -Instances $instances `
        -RequestedCount $count `
        -RequestedBasePort $requestedBasePort `
        -BindHost $bindHost `
        -WorkspaceRoot $workspaceRoot
    Write-StatusSnapshot `
        -StatusPath $statusPath `
        -Rows $rows `
        -RequestedCount $count `
        -RequestedBasePort $requestedBasePort `
        -BindHost $bindHost `
        -WorkspaceRoot $workspaceRoot `
        -LauncherStatePath $launcherStatePath
    Render-StatusPanel `
        -Rows $rows `
        -WorkspaceRoot $workspaceRoot `
        -StatusPath $statusPath `
        -LauncherStatePath $launcherStatePath

    if ($NonInteractive -and $MonitorCycles -gt 0 -and $cycle -ge $MonitorCycles) {
        break
    }

    $remainingMs = $MonitorIntervalSeconds * 1000
    while ($remainingMs -gt 0) {
        $command = Try-ReadPanelCommand
        if (-not [string]::IsNullOrWhiteSpace($command)) {
            $action = Handle-PanelCommand `
                -Command $command `
                -Instances $instances `
                -LauncherStateDir $launcherStateDir `
                -LauncherStatePath $launcherStatePath `
                -WorkspaceRoot $workspaceRoot `
                -StatusPath $statusPath
            if ($action -eq 'stop') {
                return
            }
            if ($action -eq 'quit') {
                return
            }
            if ($action -eq 'continue' -and $command -eq 'refresh') {
                break
            }
        }

        $sleepMs = [Math]::Min($remainingMs, 200)
        Start-Sleep -Milliseconds $sleepMs
        $remainingMs -= $sleepMs
    }
}
