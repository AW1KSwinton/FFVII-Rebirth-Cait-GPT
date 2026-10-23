param(
    [string]$RuntimePython = '',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArguments = @()
)

$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvRoot = Join-Path $packageRoot '.venv'
$runtimePython = if ([string]::IsNullOrWhiteSpace($RuntimePython)) {
    Join-Path $venvRoot 'Scripts\python.exe'
}
else {
    $RuntimePython
}
$usingExplicitPython = -not [string]::IsNullOrWhiteSpace($RuntimePython)

function Invoke-WorkspacePython {
    param([string[]]$Arguments)
    & $runtimePython @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Workspace Python command failed with exit code $LASTEXITCODE"
    }
}

if (-not $usingExplicitPython -and -not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
    $bootstrapPython = Get-Command py -ErrorAction SilentlyContinue
    if ($bootstrapPython) {
        & $bootstrapPython.Source -3 -m venv $venvRoot
    }
    else {
        $bootstrapPython = Get-Command python -ErrorAction SilentlyContinue
        if (-not $bootstrapPython) {
            throw 'Python 3 is required to create the workspace test environment.'
        }
        & $bootstrapPython.Source -m venv $venvRoot
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
        throw 'Could not create the workspace .venv.'
    }
}

if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
    throw "Workspace Python was not found: $runtimePython"
}

$pytestAvailable = $true
try {
    Invoke-WorkspacePython @('-c', 'import pytest')
}
catch {
    $pytestAvailable = $false
}

if (-not $pytestAvailable) {
    Write-Host 'Installing pytest into Tools\\ff7r-codex-agent-mvp\\.venv ...'
    # The test suite uses the source tree directly through the pythonpath
    # entry in pyproject.toml.  Installing the editable package here would
    # unnecessarily invoke PEP 517 build isolation (and require a second
    # network download of setuptools); pytest is the only test-runner
    # dependency needed by this wrapper.
    Invoke-WorkspacePython @('-m', 'pip', 'install', '--disable-pip-version-check', 'pytest>=8')
}

Push-Location $packageRoot
try {
    Invoke-WorkspacePython (@('-m', 'pytest') + $PytestArguments)
}
finally {
    Pop-Location
}
