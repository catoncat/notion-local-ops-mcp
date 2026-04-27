param(
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
$DesktopStatusPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Notion-MCP-status.txt'

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

function Add-Pid {
    param(
        [System.Collections.Generic.HashSet[int]]$Set,
        [int]$ProcessId
    )

    if ($ProcessId -gt 0) {
        [void]$Set.Add($ProcessId)
    }
}

function Test-ProcessAlive {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return $false
    }

    try {
        return $null -ne (Get-Process -Id $ProcessId -ErrorAction Stop)
    }
    catch {
        return $false
    }
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

function Stop-ProcessTree {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return $false
    }

    $aliveBefore = Test-ProcessAlive -ProcessId $ProcessId
    if (-not $aliveBefore) {
        return $false
    }

    try {
        & taskkill.exe /PID $ProcessId /T /F *> $null
    }
    catch {
    }
    $global:LASTEXITCODE = 0

    if (-not (Wait-ForProcessExit -ProcessId $ProcessId -TimeoutSeconds 8)) {
        try {
            Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        }
        catch {
        }
        [void](Wait-ForProcessExit -ProcessId $ProcessId -TimeoutSeconds 2)
    }

    $global:LASTEXITCODE = 0
    return $true
}

function Write-StoppedSnapshot {
    param([string]$StatusPath)

    if ([string]::IsNullOrWhiteSpace($StatusPath)) {
        return
    }

    Ensure-ParentDirectory -Path $StatusPath
    $lines = @(
        'Notion Local MCP launcher stopped.',
        ("Stopped at: {0}" -f (Get-Date).ToString('o')),
        ("Repo: {0}" -f $RepoRoot)
    )
    $lines | Set-Content -LiteralPath $StatusPath -Encoding UTF8
}

$envMap = Read-DotEnvFile -Path (Join-Path $RepoRoot '.env')
$launcherStateDir = Get-MergedConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_LAUNCHER_STATE_DIR' -Default (Join-Path $RepoRoot '.state\launcher')
$launcherStateDir = Normalize-ExistingPath -Path $launcherStateDir
$launcherStatePath = Join-Path $launcherStateDir 'active-instances.json'
$statusPath = Get-MergedConfigValue -EnvMap $envMap -Name 'NOTION_LOCAL_OPS_STATUS_PATH' -Default $DesktopStatusPath
$statusPath = Normalize-ExistingPath -Path $statusPath
$selfPid = $PID

$processIds = New-Object System.Collections.Generic.HashSet[int]

if (Test-Path -LiteralPath $launcherStatePath) {
    try {
        $state = Get-Content -LiteralPath $launcherStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($instance in @($state.instances)) {
            if ($null -ne $instance) {
                Add-Pid -Set $processIds -ProcessId ([int]$instance.server_pid)
                Add-Pid -Set $processIds -ProcessId ([int]$instance.cloudflared_pid)
            }
        }
    }
    catch {
        if (-not $Quiet) {
            Write-Host ("[WARN] Failed to read launcher state: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
        }
    }
}

$repoRootLower = $RepoRoot.ToLowerInvariant()
$scriptPatterns = @('launch-mcp-manager.ps1', 'run-mcp-instance.ps1', 'stop-mcp-manager.ps1')

foreach ($proc in Get-CimInstance Win32_Process) {
    $targetPid = [int]$proc.ProcessId
    if ($targetPid -eq $selfPid) {
        continue
    }

    $name = [string]$proc.Name
    $path = [string]$proc.ExecutablePath
    $cmd = [string]$proc.CommandLine
    $pathLower = if ([string]::IsNullOrWhiteSpace($path)) { '' } else { $path.ToLowerInvariant() }
    $cmdLower = if ([string]::IsNullOrWhiteSpace($cmd)) { '' } else { $cmd.ToLowerInvariant() }

    $matchesScript = $false
    foreach ($pattern in $scriptPatterns) {
        if ($cmdLower.Contains($pattern)) {
            $matchesScript = $true
            break
        }
    }

    $matchesCloudflared = ($name -ieq 'cloudflared.exe') -and (-not [string]::IsNullOrWhiteSpace($pathLower)) -and $pathLower.StartsWith($repoRootLower) -and ($cmdLower.Contains('tunnel --url http://127.0.0.1:') -or $cmdLower.Contains('tunnel --url http://localhost:'))
    $matchesServerBinary = ($name -ieq 'notion-local-ops-mcp.exe') -and (-not [string]::IsNullOrWhiteSpace($pathLower)) -and $pathLower.StartsWith($repoRootLower)

    if ($matchesScript -or $matchesCloudflared -or $matchesServerBinary) {
        Add-Pid -Set $processIds -ProcessId $targetPid
    }
}

$stopped = New-Object System.Collections.Generic.List[int]
foreach ($targetPid in @($processIds)) {
    if (Stop-ProcessTree -ProcessId $targetPid) {
        [void]$stopped.Add($targetPid)
    }
}

Start-Sleep -Seconds 1

if (Test-Path -LiteralPath $launcherStatePath) {
    Remove-Item -LiteralPath $launcherStatePath -Force -ErrorAction SilentlyContinue
}

Write-StoppedSnapshot -StatusPath $statusPath

if (-not $Quiet) {
    if ($stopped.Count -gt 0) {
        Write-Host ("Stopped {0} process(es)." -f $stopped.Count) -ForegroundColor Green
    }
    else {
        Write-Host 'No MCP processes were running.' -ForegroundColor Yellow
    }
    Write-Host ("Status snapshot: {0}" -f $statusPath) -ForegroundColor DarkCyan
}
