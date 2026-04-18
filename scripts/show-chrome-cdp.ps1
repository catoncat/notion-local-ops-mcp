[CmdletBinding()]
param(
    [int]$Port = 9224
)

$ErrorActionPreference = "Stop"
$baseUrl = "http://127.0.0.1:$Port"

Write-Host "CDP version endpoint: $baseUrl/json/version"
Invoke-RestMethod "$baseUrl/json/version" | Select-Object Browser,webSocketDebuggerUrl | Format-List

Write-Host ""
Write-Host "CDP target list: $baseUrl/json/list"
Invoke-RestMethod "$baseUrl/json/list" | Select-Object id,title,type,url,webSocketDebuggerUrl | Format-Table -AutoSize
