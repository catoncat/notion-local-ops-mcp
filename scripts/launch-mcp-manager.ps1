param(
    [int]$DefaultCount = 1,
    [int]$DefaultBasePort = 8766
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
$LauncherStateDir = Join-Path $RepoRoot ".state\launcher"
$LauncherInstancesDir = Join-Path $LauncherStateDir "instances"
$LauncherStatePath = Join-Path $LauncherStateDir "active-instances.json"
$DesktopStatusPath = Join-Path ([Environment]::GetFolderPath('Desktop')) "Notion-MCP-status.txt"

New-Item -ItemType Directory -Force -Path $LauncherStateDir | Out-Null
New-Item -ItemType Directory -Force -Path $LauncherInstancesDir | Out-Null

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
    return $ports
}

function Resolve-ServerExecutable {
    $candidates = @(
        (Join-Path $RepoRoot ".venv\Scripts\notion-local-ops-mcp.exe"),
        (Join-Path $RepoRoot ".venv\Scripts\python.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    throw "No MCP executable found. Expected under $RepoRoot\.venv"
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
            Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        }
        catch {
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

    [pscustomobject]@{
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
    }
}

function Read-LauncherState {
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
    if (Test-Path -LiteralPath $LauncherStatePath) {
        Remove-Item -LiteralPath $LauncherStatePath -Force
    }
}

function Write-LauncherState {
    param(
        [object[]]$Instances,
        [int]$RequestedCount,
        [int]$RequestedBasePort,
        [string]$BindHost,
        [string]$WorkspaceRoot
    )

    $payload = [ordered]@{
        launcher_version = 1
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
                }
            }
        )
    }

    $payload | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $LauncherStatePath -Encoding UTF8
}

function Convert-LauncherStateToInstances {
    param([object]$State)

    if ($null -eq $State) {
        return @()
    }

    return @(@($State.instances) | ForEach-Object { Convert-StateInstance -Item $_ })
}

function Test-InstanceHealthy {
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
        if (-not (Test-InstanceHealthy -Instance $instance)) {
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
        [string]$LogPath,
        [int]$TimeoutSeconds = 45,
        [int]$ProcessId = 0
    )

    $pattern = 'https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com'
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $content = ''
        if (Test-Path -LiteralPath $LogPath) {
            try {
                $content = Get-Content -LiteralPath $LogPath -Raw -Encoding UTF8 -ErrorAction Stop
                $matches = [regex]::Matches($content, $pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
                if ($matches.Count -gt 0) {
                    return $matches[$matches.Count - 1].Value
                }
            }
            catch {
            }
        }

        if ($ProcessId -gt 0 -and -not (Test-ProcessAlive -ProcessId $ProcessId)) {
            if ($content -match 'failed to request quick Tunnel') {
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

function New-ManagedInstance {
    param(
        [int]$Index,
        [int]$Port,
        [string]$BindHost,
        [string]$WorkspaceRoot,
        [string]$BaseStateDir,
        [string]$AuthToken,
        [string]$CodexCommand,
        [string]$ClaudeCommand,
        [int]$CommandTimeout,
        [int]$DelegateTimeout,
        [string]$ServerExecutable,
        [string]$RunnerScript,
        [string]$CloudflaredExecutable
    )

    $instanceName = "mcp-$Index"
    $instanceStateDir = Join-Path $BaseStateDir ("instance-{0}-port-{1}" -f $Index, $Port)
    $instanceDir = Join-Path $LauncherInstancesDir ("{0}-port-{1}" -f $instanceName, $Port)
    $serverLogPath = Join-Path $instanceDir 'server.log'
    $cloudflaredStdoutLogPath = Join-Path $instanceDir 'cloudflared.stdout.log'
    $cloudflaredStderrLogPath = Join-Path $instanceDir 'cloudflared.stderr.log'

    New-Item -ItemType Directory -Force -Path $instanceStateDir | Out-Null
    New-Item -ItemType Directory -Force -Path $instanceDir | Out-Null
    foreach ($path in @($serverLogPath, $cloudflaredStdoutLogPath, $cloudflaredStderrLogPath)) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
        }
    }

    $argList = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy Bypass',
        ('-File "{0}"' -f $RunnerScript),
        ('-ServerExecutable "{0}"' -f $ServerExecutable),
        ('-BindHost "{0}"' -f $BindHost),
        ('-Port {0}' -f $Port),
        ('-WorkspaceRoot "{0}"' -f $WorkspaceRoot),
        ('-StateDir "{0}"' -f $instanceStateDir),
        ('-AuthToken "{0}"' -f $AuthToken),
        ('-CodexCommand "{0}"' -f $CodexCommand),
        ('-ClaudeCommand "{0}"' -f $ClaudeCommand),
        ('-CommandTimeout {0}' -f $CommandTimeout),
        ('-DelegateTimeout {0}' -f $DelegateTimeout),
        ('-LogPath "{0}"' -f $serverLogPath)
    ) -join ' '

    $serverProcess = $null
    $cloudflaredProcess = $null
    $originUrl = "http://$BindHost`:$Port"

    try {
        $serverProcess = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru

        if (-not (Wait-ForPort -BindHost $BindHost -Port $Port -TimeoutSeconds 20)) {
            throw "MCP instance $instanceName did not become ready on $originUrl. See $serverLogPath"
        }

        $quickTunnelUrl = $null
        $launchAttempts = 3
        for ($attempt = 1; $attempt -le $launchAttempts; $attempt++) {
            Set-Content -LiteralPath $cloudflaredStdoutLogPath -Value '' -Encoding UTF8
            Set-Content -LiteralPath $cloudflaredStderrLogPath -Value '' -Encoding UTF8

            $cloudflaredProcess = Start-Process `
                -FilePath $CloudflaredExecutable `
                -ArgumentList @('tunnel', '--url', $originUrl) `
                -WorkingDirectory $RepoRoot `
                -RedirectStandardOutput $cloudflaredStdoutLogPath `
                -RedirectStandardError $cloudflaredStderrLogPath `
                -WindowStyle Hidden `
                -PassThru

            $quickTunnelUrl = Wait-ForQuickTunnelUrl -LogPath $cloudflaredStderrLogPath -TimeoutSeconds 25 -ProcessId $cloudflaredProcess.Id
            if (-not [string]::IsNullOrWhiteSpace($quickTunnelUrl)) {
                break
            }

            Stop-ManagedProcess -ProcessId $cloudflaredProcess.Id
            $cloudflaredProcess = $null
            if ($attempt -lt $launchAttempts) {
                Start-Sleep -Seconds 2
            }
        }

        if ([string]::IsNullOrWhiteSpace($quickTunnelUrl)) {
            throw "Failed to extract quick tunnel URL for $instanceName after $launchAttempts attempts. See $cloudflaredStderrLogPath"
        }

        return [pscustomobject]@{
            Name = $instanceName
            Host = $BindHost
            Port = $Port
            Token = $AuthToken
            LocalUrl = "$originUrl/mcp"
            PublicUrl = $quickTunnelUrl
            PublicMcpUrl = "$quickTunnelUrl/mcp"
            ServerProcessId = $serverProcess.Id
            CloudflaredProcessId = $cloudflaredProcess.Id
            StateDir = $instanceStateDir
            InstanceDir = $instanceDir
            ServerLogPath = $serverLogPath
            CloudflaredStdoutLogPath = $cloudflaredStdoutLogPath
            CloudflaredStderrLogPath = $cloudflaredStderrLogPath
            StartedAt = (Get-Date).ToString('o')
        }
    }
    catch {
        if ($null -ne $cloudflaredProcess) {
            Stop-ManagedProcess -ProcessId $cloudflaredProcess.Id
        }
        if ($null -ne $serverProcess) {
            Stop-ManagedProcess -ProcessId $serverProcess.Id
        }
        throw
    }
}

function Get-InstanceStatus {
    param([pscustomobject]$Instance)

    $serverAlive = Test-ProcessAlive -ProcessId ([int]$Instance.ServerProcessId)
    $tunnelAlive = Test-ProcessAlive -ProcessId ([int]$Instance.CloudflaredProcessId)
    $listening = Test-PortListening -BindHost $Instance.Host -Port $Instance.Port

    $status = if ($serverAlive -and $listening -and $tunnelAlive) {
        'Running'
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

    [pscustomobject]@{
        Instance = $Instance.Name
        ServerPID = $Instance.ServerProcessId
        TunnelPID = $Instance.CloudflaredProcessId
        Port = $Instance.Port
        Status = $status
        Token = $Instance.Token
        LocalUrl = $Instance.LocalUrl
        PublicUrl = $Instance.PublicUrl
        PublicMcpUrl = $Instance.PublicMcpUrl
        ServerLog = $Instance.ServerLogPath
        CloudflaredStdoutLog = $Instance.CloudflaredStdoutLogPath
        CloudflaredStderrLog = $Instance.CloudflaredStderrLogPath
    }
}

function Write-StoppedSnapshot {
    param([string]$WorkspaceRoot)

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add(("Updated: {0}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')))
    $lines.Add(("Repo: {0}" -f $RepoRoot))
    $lines.Add(("Workspace: {0}" -f $WorkspaceRoot))
    $lines.Add('')
    $lines.Add('All managed MCP instances and Cloudflare tunnels have been stopped.')
    $lines.Add(("State File: {0}" -f $LauncherStatePath))
    Ensure-ParentDirectory -Path $DesktopStatusPath
    Set-Content -LiteralPath $DesktopStatusPath -Value $lines -Encoding UTF8
}

function Read-PanelCommand {
    param([string]$Prompt = 'Command')

    try {
        $raw = Read-Host $Prompt
        if ($null -eq $raw) {
            return ''
        }
        return $raw.Trim()
    }
    catch {
        return 'quit'
    }
}

function Write-StatusSnapshot {
    param(
        [object[]]$Rows,
        [int]$RequestedCount,
        [int]$RequestedBasePort,
        [string]$BindHost,
        [string]$WorkspaceRoot
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
        $lines.Add(("  Server PID: {0}" -f $row.ServerPID))
        $lines.Add(("  cloudflared PID: {0}" -f $row.TunnelPID))
        $lines.Add(("  Server Log: {0}" -f $row.ServerLog))
        $lines.Add(("  Tunnel stdout log: {0}" -f $row.CloudflaredStdoutLog))
        $lines.Add(("  Tunnel stderr log: {0}" -f $row.CloudflaredStderrLog))
        $lines.Add('')
    }

    Ensure-ParentDirectory -Path $DesktopStatusPath
    Set-Content -LiteralPath $DesktopStatusPath -Value $lines -Encoding UTF8
}

# Main
$envMap = Read-DotEnvFile -Path (Join-Path $RepoRoot '.env')
$bindHost = if ($envMap.ContainsKey('NOTION_LOCAL_OPS_HOST')) { $envMap['NOTION_LOCAL_OPS_HOST'] } else { '127.0.0.1' }
$basePort = if ($envMap.ContainsKey('NOTION_LOCAL_OPS_PORT')) { [int]$envMap['NOTION_LOCAL_OPS_PORT'] } else { $DefaultBasePort }
$workspaceRoot = if ($envMap.ContainsKey('NOTION_LOCAL_OPS_WORKSPACE_ROOT')) { $envMap['NOTION_LOCAL_OPS_WORKSPACE_ROOT'] } else { $RepoRoot }
$baseStateDir = if ($envMap.ContainsKey('NOTION_LOCAL_OPS_STATE_DIR')) { $envMap['NOTION_LOCAL_OPS_STATE_DIR'] } else { (Join-Path $RepoRoot '.state\mcp-instances') }
$authToken = if ($envMap.ContainsKey('NOTION_LOCAL_OPS_AUTH_TOKEN')) { $envMap['NOTION_LOCAL_OPS_AUTH_TOKEN'] } else { '' }
$secondAuthToken = if ($envMap.ContainsKey('NOTION_LOCAL_OPS_AUTH_TOKEN_SECOND')) { $envMap['NOTION_LOCAL_OPS_AUTH_TOKEN_SECOND'] } else { $authToken }
$cloudflaredCommand = if ($envMap.ContainsKey('NOTION_LOCAL_OPS_CLOUDFLARED_COMMAND')) { $envMap['NOTION_LOCAL_OPS_CLOUDFLARED_COMMAND'] } else { '' }
$statusPath = if ($envMap.ContainsKey('NOTION_LOCAL_OPS_STATUS_PATH')) { $envMap['NOTION_LOCAL_OPS_STATUS_PATH'] } else { $DesktopStatusPath }
$codexCommand = if ($envMap.ContainsKey('NOTION_LOCAL_OPS_CODEX_COMMAND')) { $envMap['NOTION_LOCAL_OPS_CODEX_COMMAND'] } else { 'codex' }
$claudeCommand = if ($envMap.ContainsKey('NOTION_LOCAL_OPS_CLAUDE_COMMAND')) { $envMap['NOTION_LOCAL_OPS_CLAUDE_COMMAND'] } else { 'claude' }
$commandTimeout = if ($envMap.ContainsKey('NOTION_LOCAL_OPS_COMMAND_TIMEOUT')) { [int]$envMap['NOTION_LOCAL_OPS_COMMAND_TIMEOUT'] } else { 30 }
$delegateTimeout = if ($envMap.ContainsKey('NOTION_LOCAL_OPS_DELEGATE_TIMEOUT')) { [int]$envMap['NOTION_LOCAL_OPS_DELEGATE_TIMEOUT'] } else { 1800 }
$DesktopStatusPath = Normalize-ExistingPath -Path $statusPath

Clear-Host
Write-Host '=== Notion Local MCP Launcher ===' -ForegroundColor Cyan
Write-Host "Repo: $RepoRoot"
Write-Host "Workspace: $workspaceRoot"
Write-Host "Base port: $basePort"
Write-Host ''

$count = Read-IntWithDefault -Prompt 'How many MCP instances?' -Default $DefaultCount
$requestedBasePort = Read-IntWithDefault -Prompt 'Starting port?' -Default $basePort

if (-not (Test-Path -LiteralPath $workspaceRoot)) {
    throw "Workspace does not exist: $workspaceRoot"
}

$instances = @()
$existingState = Read-LauncherState
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
        Write-LauncherState `
            -Instances $instances `
            -RequestedCount $count `
            -RequestedBasePort $requestedBasePort `
            -BindHost $bindHost `
            -WorkspaceRoot $workspaceRoot
    }
    else {
        if ($stateHealthy) {
            Write-Host 'Existing launcher state does not match the requested configuration. Restarting managed instances...' -ForegroundColor Yellow
        }
        else {
            Write-Host 'Found stale launcher state. Cleaning up stale processes...' -ForegroundColor Yellow
        }
        Stop-ManagedInstances -Instances $stateInstances
        Remove-LauncherState
    }
}

if ($instances.Count -eq 0) {
    $serverExecutable = Resolve-ServerExecutable
    $runnerScript = Join-Path $PSScriptRoot 'run-mcp-instance.ps1'
    if (-not (Test-Path -LiteralPath $runnerScript)) {
        throw "Missing runner script: $runnerScript"
    }
    $cloudflaredExecutable = Resolve-CloudflaredExecutable -PreferredCommand $cloudflaredCommand

    Write-Host "Cloudflared: $cloudflaredExecutable"
    $ports = Get-FreePorts -Count $count -StartingPort $requestedBasePort
    $startedInstances = New-Object System.Collections.Generic.List[object]

    try {
        for ($i = 0; $i -lt $ports.Count; $i++) {
            $token = Get-RequestedInstanceToken -Index ($i + 1) -PrimaryToken $authToken -SecondToken $secondAuthToken
            $instance = New-ManagedInstance `
                -Index ($i + 1) `
                -Port $ports[$i] `
                -BindHost $bindHost `
                -WorkspaceRoot $workspaceRoot `
                -BaseStateDir $baseStateDir `
                -AuthToken $token `
                -CodexCommand $codexCommand `
                -ClaudeCommand $claudeCommand `
                -CommandTimeout $commandTimeout `
                -DelegateTimeout $delegateTimeout `
                -ServerExecutable $serverExecutable `
                -RunnerScript $runnerScript `
                -CloudflaredExecutable $cloudflaredExecutable
            [void]$startedInstances.Add($instance)
        }

        $instances = @($startedInstances.ToArray())
        Write-LauncherState `
            -Instances $instances `
            -RequestedCount $count `
            -RequestedBasePort $requestedBasePort `
            -BindHost $bindHost `
            -WorkspaceRoot $workspaceRoot
    }
    catch {
        Stop-ManagedInstances -Instances @($startedInstances.ToArray())
        Remove-LauncherState
        throw
    }
}

Write-Host ''
Write-Host 'Instances ready. Checking status...' -ForegroundColor Green
Start-Sleep -Seconds 1

while ($true) {
    Clear-Host
    Write-Host '=== Notion Local MCP Status ===' -ForegroundColor Cyan
    Write-Host ("Repo: {0}" -f $RepoRoot)
    Write-Host ("Workspace: {0}" -f $workspaceRoot)
    Write-Host ("Desktop status file: {0}" -f $DesktopStatusPath)
    Write-Host ("State file: {0}" -f $LauncherStatePath)
    Write-Host ''

    $rows = @($instances | ForEach-Object { Get-InstanceStatus -Instance $_ })
    Write-StatusSnapshot `
        -Rows $rows `
        -RequestedCount $count `
        -RequestedBasePort $requestedBasePort `
        -BindHost $bindHost `
        -WorkspaceRoot $workspaceRoot

    $rows | Format-Table -AutoSize Instance,ServerPID,TunnelPID,Port,Status,Token,PublicMcpUrl

    Write-Host ''
    Write-Host 'Details:' -ForegroundColor DarkCyan
    foreach ($row in $rows) {
        Write-Host ("- {0}" -f $row.Instance) -ForegroundColor DarkCyan
        Write-Host ("    Local URL: {0}" -f $row.LocalUrl)
        Write-Host ("    Public URL: {0}" -f $row.PublicUrl)
        Write-Host ("    Public MCP URL: {0}" -f $row.PublicMcpUrl)
        Write-Host ("    Server Log: {0}" -f $row.ServerLog)
        Write-Host ("    Tunnel stdout log: {0}" -f $row.CloudflaredStdoutLog)
        Write-Host ("    Tunnel stderr log: {0}" -f $row.CloudflaredStderrLog)
    }
    Write-Host ''
    Write-Host 'Commands: [Enter]=refresh  stop=shutdown all  logs=open state/log folder  quit=close panel only' -ForegroundColor Yellow
    Write-Host ('State/log folder: {0}' -f $LauncherStateDir) -ForegroundColor DarkYellow

    $command = Read-PanelCommand -Prompt 'Panel command'
    switch ($command.ToLowerInvariant()) {
        '' {
            continue
        }
        'refresh' {
            continue
        }
        'logs' {
            Start-Process explorer.exe $LauncherStateDir | Out-Null
            continue
        }
        'stop' {
            Write-Host ''
            Write-Host 'Stopping all managed MCP instances and Cloudflare tunnels...' -ForegroundColor Yellow
            Stop-ManagedInstances -Instances $instances
            Remove-LauncherState
            Write-StoppedSnapshot -WorkspaceRoot $workspaceRoot
            Write-Host 'All managed MCP instances and tunnels have been stopped.' -ForegroundColor Green
            return
        }
        'quit' {
            return
        }
        default {
            Write-Host ("Unknown command: {0}" -f $command) -ForegroundColor Red
            Write-Host 'Valid commands: refresh, stop, logs, quit' -ForegroundColor Yellow
            Start-Sleep -Seconds 1
            continue
        }
    }
}
