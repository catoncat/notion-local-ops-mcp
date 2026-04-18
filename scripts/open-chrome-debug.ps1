[CmdletBinding()]
param(
    [string]$Url = "about:blank",
    [int]$Port = 9224,
    [string]$ChromePath,
    [string]$UserDataDir
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Get-ChromePath {
    param([string]$ExplicitPath)

    if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
        if (-not (Test-Path -LiteralPath $ExplicitPath)) {
            throw "Chrome not found: $ExplicitPath"
        }
        return $ExplicitPath
    }

    foreach ($candidate in @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    )) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    $command = Get-Command "chrome" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    throw "Google Chrome was not found."
}

if ([string]::IsNullOrWhiteSpace($UserDataDir)) {
    $UserDataDir = Join-Path $env:TEMP "notion-local-ops-chrome-debug"
}

New-Item -ItemType Directory -Force -Path $UserDataDir | Out-Null
$resolvedChrome = Get-ChromePath -ExplicitPath $ChromePath

$argumentLine = "--remote-debugging-port=$Port --user-data-dir=$UserDataDir --no-first-run --no-default-browser-check $Url"
$process = Start-Process -FilePath $resolvedChrome -ArgumentList $argumentLine -PassThru

Write-Host "Chrome started."
Write-Host "PID: $($process.Id)"
Write-Host "Remote debugging: http://127.0.0.1:$Port"
Write-Host "Version endpoint: http://127.0.0.1:$Port/json/version"
Write-Host "Profile dir: $UserDataDir"
Write-Host "Opened URL: $Url"
