[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StatePath = Join-Path $RootDir ".state\quick-tunnel-state.json"

if (-not (Test-Path -LiteralPath $StatePath)) {
    Write-Host "Quick tunnel is not running (no state file)."
    exit 0
}

$state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
$serverAlive = $null -ne (Get-Process -Id $state.server_pid -ErrorAction SilentlyContinue)
$cloudflaredAlive = $null -ne (Get-Process -Id $state.cloudflared_pid -ErrorAction SilentlyContinue)

[pscustomobject]@{
    StartedAt = $state.started_at
    ServerPid = $state.server_pid
    ServerAlive = $serverAlive
    CloudflaredPid = $state.cloudflared_pid
    CloudflaredAlive = $cloudflaredAlive
    LocalUrl = $state.local_url
    PublicUrl = $state.public_url
    McpUrl = $state.mcp_url
    UrlFile = $state.url_file
    ServerLog = $state.server_stderr_log
    CloudflaredLog = $state.cloudflared_stderr_log
} | Format-List
