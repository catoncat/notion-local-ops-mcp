[CmdletBinding()]
param(
    [switch]$AutoConnect,
    [string]$Channel,
    [string]$BrowserUrl = "http://127.0.0.1:9224"
)

$ErrorActionPreference = "Stop"

$npx = Get-Command "npx" -ErrorAction SilentlyContinue
if (-not $npx) {
    throw "npx not found. Install Node.js first."
}

$args = @("-y", "chrome-devtools-mcp@latest")
if ($AutoConnect) {
    $args += "--autoConnect"
} else {
    $args += "--browserUrl"
    $args += $BrowserUrl
}
if (-not [string]::IsNullOrWhiteSpace($Channel)) {
    $args += "--channel=$Channel"
}

Write-Host "Running: npx $($args -join ' ')"
& $npx.Source @args
