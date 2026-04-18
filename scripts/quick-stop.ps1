[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StatePath = Join-Path $RootDir ".state\quick-tunnel-state.json"

function Stop-StrayManagedProcesses {
    $commandPattern = 'notion_local_ops_mcp\.server|cloudflared\.exe"\s+tunnel\s+--url\s+http://127\.0\.0\.1:8766'

    $stopped = New-Object System.Collections.Generic.List[int]
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -match 'python|cloudflared' -and
            $_.CommandLine -match $commandPattern
        } |
        ForEach-Object {
            try {
                Stop-Process -Id $_.ProcessId -Force
                [void]$stopped.Add([int]$_.ProcessId)
            } catch {
            }
        }

    return $stopped
}

$stopped = @()

if (Test-Path -LiteralPath $StatePath) {
    $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    foreach ($managedProcessId in @($state.cloudflared_pid, $state.server_pid)) {
        if ($managedProcessId -and (Get-Process -Id $managedProcessId -ErrorAction SilentlyContinue)) {
            Stop-Process -Id $managedProcessId -Force
            $stopped += $managedProcessId
        }
    }

    Remove-Item -LiteralPath $StatePath -Force
}

$stopped += Stop-StrayManagedProcesses
$stopped = $stopped | Sort-Object -Unique

if ($stopped.Count -eq 0) {
    Write-Host "No quick tunnel processes found."
} else {
    Write-Host "Stopped PIDs: $($stopped -join ', ')"
}

Write-Host "State cleared: $StatePath"
