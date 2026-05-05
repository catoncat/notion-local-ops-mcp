param(
    [Parameter(Mandatory = $true)][string]$ServerExecutable,
    [string]$BindHost = '127.0.0.1',
    [Parameter(Mandatory = $true)][int]$Port,
    [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
    [Parameter(Mandatory = $true)][string]$StateDir,
    [string]$AuthToken = '',
    [string]$CodexCommand = 'codex',
    [string]$ClaudeCommand = 'claude',
    [int]$CommandTimeout = 30,
    [int]$DelegateTimeout = 1800,
    [Parameter(Mandatory = $true)][string]$LogPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

$env:NOTION_LOCAL_OPS_HOST = $BindHost
$env:NOTION_LOCAL_OPS_PORT = [string]$Port
$env:NOTION_LOCAL_OPS_WORKSPACE_ROOT = $WorkspaceRoot
$env:NOTION_LOCAL_OPS_STATE_DIR = $StateDir
$env:NOTION_LOCAL_OPS_AUTH_TOKEN = $AuthToken
$env:NOTION_LOCAL_OPS_CODEX_COMMAND = $CodexCommand
$env:NOTION_LOCAL_OPS_CLAUDE_COMMAND = $ClaudeCommand
$env:NOTION_LOCAL_OPS_COMMAND_TIMEOUT = [string]$CommandTimeout
$env:NOTION_LOCAL_OPS_DELEGATE_TIMEOUT = [string]$DelegateTimeout

$banner = @(
    ('=' * 72),
    ("[{0}] Starting MCP instance" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')),
    ("Executable: {0}" -f $ServerExecutable),
    ("URL: http://{0}:{1}/mcp" -f $BindHost, $Port),
    ("Workspace: {0}" -f $WorkspaceRoot),
    ("StateDir: {0}" -f $StateDir),
    ('=' * 72)
)
$banner | Out-File -LiteralPath $LogPath -Encoding UTF8 -Append

$exitCode = 1

try {
    if ($ServerExecutable.ToLowerInvariant().EndsWith('python.exe')) {
        $workingDir = Split-Path -Parent (Split-Path -Parent $ServerExecutable)
    }
    else {
        $workingDir = Split-Path -Parent $ServerExecutable
    }

    Push-Location $workingDir
    try {
        if ($ServerExecutable.ToLowerInvariant().EndsWith('python.exe')) {
            $commandLine = '"{0}" -m notion_local_ops_mcp.server >> "{1}" 2>&1' -f $ServerExecutable, $LogPath
        }
        else {
            $commandLine = '"{0}" >> "{1}" 2>&1' -f $ServerExecutable, $LogPath
        }

        cmd.exe /d /c $commandLine
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
catch {
    $_ | Out-File -LiteralPath $LogPath -Encoding UTF8 -Append
    $exitCode = 1
}
finally {
    @(
        ('-' * 72),
        ("[{0}] MCP instance exited with code {1}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $exitCode),
        ('-' * 72)
    ) | Out-File -LiteralPath $LogPath -Encoding UTF8 -Append
}

exit $exitCode
