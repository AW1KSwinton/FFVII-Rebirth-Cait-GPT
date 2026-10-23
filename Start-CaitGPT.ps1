[CmdletBinding()]
param(
    [ValidateSet('simulation', 'windows', 'windows_gamepad')]
    [string]$InputBackend = 'simulation',
    [string]$TelemetryFile,
    [string]$GameExecutable,
    [string]$RuntimePython = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    [ValidateRange(1024, 65535)] [int]$Port = 8766,
    [string]$ExpectedRef,
    [string]$ExpectedCommit
)
$ErrorActionPreference = 'Stop'
if ($InputBackend -ne 'simulation') {
    if (-not $TelemetryFile -or -not (Test-Path -LiteralPath $TelemetryFile -PathType Leaf)) {
        throw 'Real input requires an explicit existing -TelemetryFile.'
    }
    if (-not $GameExecutable -or -not (Test-Path -LiteralPath $GameExecutable -PathType Leaf)) {
        throw 'Real input requires an explicit existing -GameExecutable.'
    }
}
if ([bool]$ExpectedRef -ne [bool]$ExpectedCommit) {
    throw 'Supply both -ExpectedRef and -ExpectedCommit, or neither.'
}
if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is required on Windows.' }
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'ff7r-cait-gpt'
$env:FF7R_AUTONOMY_RUNTIME_DIR = $runtimeRoot
$env:FF7R_MCP_HTTP_RUNTIME_DIR = Join-Path $runtimeRoot 'mcp-http'
$env:FF7R_INPUT_BACKEND = $InputBackend
if ($InputBackend -eq 'simulation') {
    # Ignore an inherited live telemetry path for an explicitly simulated session.
    Remove-Item Env:FF7R_TELEMETRY_FILE -ErrorAction SilentlyContinue
} else {
    $env:FF7R_TELEMETRY_FILE = (Resolve-Path -LiteralPath $TelemetryFile).Path
    $env:FF7R_GAME_EXECUTABLE = (Resolve-Path -LiteralPath $GameExecutable).Path
}
$arguments = @{ RuntimePython=$RuntimePython; Port=$Port }
if ($ExpectedRef) {
    $arguments.RuntimeProfile = 'FeatureValidation'
    $arguments.RepositoryRoot = $PSScriptRoot
    $arguments.ExpectedRef = $ExpectedRef
    $arguments.ExpectedCommitSha = $ExpectedCommit
}
& (Join-Path $PSScriptRoot 'Tools\ff7r-codex-agent-mvp\Start-FF7RHttpMcp.ps1') @arguments
