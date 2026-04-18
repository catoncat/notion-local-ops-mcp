[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StatePath = Join-Path $RootDir ".state\quick-dual-tunnel-state.json"

function Stop-IfRunning {
    param([int]$ProcessId)

    if ($ProcessId -and (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $ProcessId -Force
        return $true
    }

    return $false
}

if (-not (Test-Path -LiteralPath $StatePath)) {
    Write-Host "No dual quick-tunnel state file found."
    exit 0
}

$state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
$stopped = New-Object System.Collections.Generic.List[string]

foreach ($instance in @($state.instances)) {
    foreach ($processInfo in @(
        @{ Kind = "server"; Id = [int]$instance.server_pid },
        @{ Kind = "cloudflared"; Id = [int]$instance.cloudflared_pid }
    )) {
        if (Stop-IfRunning -ProcessId $processInfo.Id) {
            [void]$stopped.Add("$($instance.name) $($processInfo.Kind) PID $($processInfo.Id)")
        }
    }
}

Remove-Item -LiteralPath $StatePath -Force

if ($stopped.Count -eq 0) {
    Write-Host "No dual quick-tunnel processes were still running."
} else {
    Write-Host "Stopped:"
    $stopped | ForEach-Object { Write-Host " - $_" }
}

Write-Host "State cleared: $StatePath"
