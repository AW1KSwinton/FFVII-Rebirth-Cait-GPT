[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$RuntimePython = '',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$MissionArguments = @()
)
$ErrorActionPreference = 'Stop'
$navPackage = Split-Path -Parent $MyInvocation.MyCommand.Path
$navRepo = Split-Path -Parent (Split-Path -Parent $navPackage)
if ([string]::IsNullOrWhiteSpace($RuntimePython)) {
    $navManifest = Get-Content -LiteralPath (Join-Path $navRepo 'ProjectKnowledge\Current\canonical-project-state.json') -Raw | ConvertFrom-Json
    $RuntimePython = $navManifest.installed_editable_runtime.python
}
if (-not (Test-Path -LiteralPath $RuntimePython -PathType Leaf)) {
    throw "Runtime Python was not found: $RuntimePython"
}
$navPreviousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $navPackage 'src'
    & $RuntimePython -u -m ff7r_agent.navigation_mission @MissionArguments
    if ($LASTEXITCODE -ne 0) { throw "Navigation mission stopped; see its result.json (exit $LASTEXITCODE)." }
}
finally {
    $env:PYTHONPATH = $navPreviousPythonPath
}
