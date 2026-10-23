param(
    [string]$RuntimePython = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    [switch]$EnableGamepad
)

$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Test-Path -LiteralPath $RuntimePython -PathType Leaf)) {
    throw "FF7R runtime Python was not found: $RuntimePython"
}

# The MCP executable must be stopped before pip can replace its entry point.
# Keep this check explicit so an active Codex session is never interrupted.
$mcpProcesses = Get-Process -Name 'ff7r-agent-mcp' -ErrorAction SilentlyContinue
if ($mcpProcesses) {
    throw "ff7r-agent-mcp is still running; restart Codex/MCP, then rerun this script."
}

# The active game must also be closed before changing the runtime package.
# This keeps the installed controller and the running game on a known pair.
$gameProcesses = Get-Process -Name 'ff7rebirth_', 'ff7rebirth' -ErrorAction SilentlyContinue
if ($gameProcesses) {
    throw "FFVII Rebirth is still running; close the game, then rerun this script."
}

$editableTarget = "$packageRoot[dev,windows]"
& $RuntimePython -m pip install --no-deps --editable $editableTarget
if ($LASTEXITCODE -ne 0) {
    throw "Workspace package installation failed with exit code $LASTEXITCODE"
}

if ($EnableGamepad) {
    # Suppress vgamepad's optional driver-install side effect. Driver
    # installation is an explicit machine prerequisite, not an implicit
    # workspace refresh mutation.
    $oldSkipDriverInstall = $env:VGAMEPAD_SKIP_VIGEMBUS_INSTALL
    $env:VGAMEPAD_SKIP_VIGEMBUS_INSTALL = 'true'
    try {
        & $RuntimePython -m pip install --no-deps vgamepad
        if ($LASTEXITCODE -ne 0) {
            throw "vgamepad Python dependency installation failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        if ($null -eq $oldSkipDriverInstall) {
            Remove-Item Env:VGAMEPAD_SKIP_VIGEMBUS_INSTALL -ErrorAction SilentlyContinue
        }
        else {
            $env:VGAMEPAD_SKIP_VIGEMBUS_INSTALL = $oldSkipDriverInstall
        }
    }
}

& $RuntimePython -c "import ff7r_agent, ff7r_agent.mcp_runtime_server, ff7r_agent.runtime_worker, ff7r_agent.runtime_identity; print(ff7r_agent.__file__); print(ff7r_agent.mcp_runtime_server.__file__); print(ff7r_agent.runtime_worker.__file__); print(ff7r_agent.runtime_identity.__file__)"
if ($LASTEXITCODE -ne 0) {
    throw "Installed package import verification failed with exit code $LASTEXITCODE"
}
