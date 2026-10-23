[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CanonicalRepositoryRoot,
    [string]$RuntimePython = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    [string]$BindHost = '127.0.0.1',
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $CanonicalRepositoryRoot -PathType Container)) {
    throw "Canonical repository root does not exist: $CanonicalRepositoryRoot"
}
$canonicalRoot = (Resolve-Path -LiteralPath $CanonicalRepositoryRoot).Path
$stopScript = Join-Path $canonicalRoot 'Tools\ff7r-codex-agent-mvp\Stop-FF7RHttpMcp.ps1'
$startScript = Join-Path $canonicalRoot 'Tools\ff7r-codex-agent-mvp\Start-FF7RHttpMcp.ps1'
if (-not (Test-Path -LiteralPath $stopScript -PathType Leaf) -or -not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
    throw "Canonical launcher/stop scripts are missing beneath $canonicalRoot"
}

# The caller must have already queried get_agent_runtime_status and
# get_runtime_identity and confirmed no async job is active. Stop-FF7RHttpMcp
# still performs the authenticated, identity-checked graceful close and will
# refuse an untrusted listener or an unreleased port.
& $stopScript -BindHost $BindHost -Port $Port
if ($LASTEXITCODE -ne 0) {
    throw "Canonical restoration stopped before launch because the managed bridge did not close cleanly."
}
& $startScript -RuntimePython $RuntimePython -BindHost $BindHost -Port $Port
if ($LASTEXITCODE -ne 0) {
    throw "Canonical restoration failed to start the strict canonical launcher."
}
Write-Output "Canonical FF7R HTTP MCP restoration completed from $canonicalRoot at http://$BindHost`:$Port/mcp"
