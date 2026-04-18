[CmdletBinding()]
param(
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ToolsDir = Join-Path $RootDir "tools"
New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $ToolsDir "cloudflared.exe"
}

$downloadUrl = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
Write-Host "Downloading cloudflared from $downloadUrl"
Invoke-WebRequest -Uri $downloadUrl -OutFile $OutputPath

Write-Host "Saved to: $OutputPath"
& $OutputPath --version
