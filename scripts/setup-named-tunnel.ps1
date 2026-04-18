[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Hostname,
    [string]$TunnelName = "notion-local-ops-mcp",
    [int]$Port = 8766,
    [switch]$OverwriteDns
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RootDir

function Get-CloudflaredCommand {
    $localBinary = Join-Path $RootDir "tools\cloudflared.exe"
    if (Test-Path -LiteralPath $localBinary) {
        return $localBinary
    }

    $command = Get-Command "cloudflared" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    throw "Missing required command: cloudflared"
}

function Get-OriginCertPath {
    $homeCloudflared = Join-Path $env:USERPROFILE ".cloudflared"
    return Join-Path $homeCloudflared "cert.pem"
}

function Ensure-OriginCert {
    param([string]$CloudflaredCommand)

    $certPath = Get-OriginCertPath
    if (Test-Path -LiteralPath $certPath) {
        return $certPath
    }

    Write-Host "No Cloudflare origin cert found at $certPath"
    Write-Host "Starting 'cloudflared tunnel login' now. Complete the browser flow, then rerun this command."
    & $CloudflaredCommand tunnel login

    if (-not (Test-Path -LiteralPath $certPath)) {
        throw "Cloudflare login did not create $certPath"
    }

    return $certPath
}

function Get-TunnelInfo {
    param(
        [string]$CloudflaredCommand,
        [string]$Name
    )

    $json = & $CloudflaredCommand tunnel list -n $Name -o json
    if (-not $json) {
        return $null
    }

    $items = $json | ConvertFrom-Json
    if (-not $items -or $items.Count -eq 0) {
        return $null
    }

    return $items[0]
}

function Ensure-CredentialsFile {
    param(
        [string]$CloudflaredCommand,
        [string]$Name
    )

    $credDir = Join-Path $env:USERPROFILE ".cloudflared"
    New-Item -ItemType Directory -Force -Path $credDir | Out-Null
    $credPath = Join-Path $credDir "$Name.json"

    & $CloudflaredCommand tunnel token --cred-file $credPath $Name | Out-Null

    if (-not (Test-Path -LiteralPath $credPath)) {
        throw "Failed to write credentials file: $credPath"
    }

    return $credPath
}

function Write-NamedTunnelConfig {
    param(
        [string]$TunnelId,
        [string]$CredentialsFile,
        [string]$Hostname,
        [int]$Port
    )

    $configPath = Join-Path $RootDir "cloudflared.local.yml"
    $yaml = @(
        "tunnel: $TunnelId"
        "credentials-file: $CredentialsFile"
        ""
        "ingress:"
        "  - hostname: $Hostname"
        "    service: http://127.0.0.1:$Port"
        "  - service: http_status:404"
        ""
    ) -join "`r`n"

    Set-Content -LiteralPath $configPath -Value $yaml -Encoding UTF8
    return $configPath
}

function Update-DotEnv {
    param([string]$ConfigPath)

    $envPath = Join-Path $RootDir ".env"
    if (-not (Test-Path -LiteralPath $envPath)) {
        throw ".env not found: $envPath"
    }

    $content = Get-Content -LiteralPath $envPath -Raw
    $content = [regex]::Replace(
        $content,
        '(?m)^NOTION_LOCAL_OPS_CLOUDFLARED_CONFIG=.*$',
        'NOTION_LOCAL_OPS_CLOUDFLARED_CONFIG="cloudflared.local.yml"'
    )
    $content = [regex]::Replace(
        $content,
        '(?m)^NOTION_LOCAL_OPS_TUNNEL_NAME=.*$',
        'NOTION_LOCAL_OPS_TUNNEL_NAME='
    )
    Set-Content -LiteralPath $envPath -Value $content -Encoding UTF8
}

$cloudflared = Get-CloudflaredCommand
$null = Ensure-OriginCert -CloudflaredCommand $cloudflared

$tunnel = Get-TunnelInfo -CloudflaredCommand $cloudflared -Name $TunnelName
if (-not $tunnel) {
    Write-Host "Creating named tunnel: $TunnelName"
    & $cloudflared tunnel create $TunnelName | Out-Host
    $tunnel = Get-TunnelInfo -CloudflaredCommand $cloudflared -Name $TunnelName
    if (-not $tunnel) {
        throw "Tunnel creation succeeded but tunnel lookup failed for $TunnelName"
    }
} else {
    Write-Host "Reusing existing tunnel: $TunnelName ($($tunnel.id))"
}

$credFile = Ensure-CredentialsFile -CloudflaredCommand $cloudflared -Name $TunnelName

$routeArgs = @("tunnel", "route", "dns")
if ($OverwriteDns) {
    $routeArgs += "--overwrite-dns"
}
$routeArgs += @($TunnelName, $Hostname)
& $cloudflared @routeArgs | Out-Host

$configPath = Write-NamedTunnelConfig `
    -TunnelId $tunnel.id `
    -CredentialsFile $credFile `
    -Hostname $Hostname `
    -Port $Port

Update-DotEnv -ConfigPath $configPath

Write-Host ""
Write-Host "Named tunnel is configured."
Write-Host "Tunnel name: $TunnelName"
Write-Host "Tunnel id: $($tunnel.id)"
Write-Host "Hostname: $Hostname"
Write-Host "Config: $configPath"
Write-Host "Credentials: $credFile"
Write-Host "Next: run .\\scripts\\dev-tunnel.ps1"
