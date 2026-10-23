[CmdletBinding()]
param(
    [string]$RuntimePython = (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    [string]$BindHost = '127.0.0.1',
    [int]$Port = 8765,
    [ValidateSet('Canonical', 'FeatureValidation')]
    [string]$RuntimeProfile = 'Canonical',
    [string]$RepositoryRoot = $null,
    [string]$ExpectedCommitSha = $null,
    [string]$ExpectedRef = $null,
    [ValidateRange(1, 120)]
    [int]$StartupTimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'
$derivedRepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if ($RuntimeProfile -eq 'Canonical') {
    if ($RepositoryRoot -or $ExpectedCommitSha -or $ExpectedRef) {
        throw 'Canonical profile does not accept -RepositoryRoot, -ExpectedCommitSha, or -ExpectedRef.'
    }
    $repoRoot = $derivedRepoRoot
    $profileName = 'CANONICAL'
} else {
    if (-not $RepositoryRoot -or -not $ExpectedCommitSha -or -not $ExpectedRef) {
        throw 'FeatureValidation requires explicit -RepositoryRoot, -ExpectedCommitSha, and -ExpectedRef.'
    }
    if (-not (Test-Path -LiteralPath $RepositoryRoot -PathType Container)) {
        throw "FeatureValidation repository root does not exist: $RepositoryRoot"
    }
    $repoRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
    $profileName = 'FEATURE_VALIDATION'
}
$safeRepoRoot = $repoRoot.Replace('\', '/')
$manifestPath = Join-Path $repoRoot 'ProjectKnowledge\Current\canonical-project-state.json'
$runtimeDir = if ($env:FF7R_MCP_HTTP_RUNTIME_DIR) { $env:FF7R_MCP_HTTP_RUNTIME_DIR } else { Join-Path $env:LOCALAPPDATA 'ff7r-cait-gpt\mcp-http' }
$env:FF7R_MCP_HTTP_RUNTIME_DIR = $runtimeDir
$pidPath = Join-Path $runtimeDir 'server.pid'
$identityPath = Join-Path $runtimeDir 'server.identity.json'
$stdoutPath = Join-Path $runtimeDir 'server.stdout.log'
$stderrPath = Join-Path $runtimeDir 'server.stderr.log'
$deploymentRecordPath = if ($profileName -eq 'FEATURE_VALIDATION') {
    Join-Path $env:LOCALAPPDATA 'ff7r-cait-gpt\feature-validation-deployment-record.json'
} else {
    Join-Path $env:LOCALAPPDATA 'ff7r-cait-gpt\deployment-record.json'
}
$url = "http://$BindHost`:$Port/mcp"
$mutex = New-Object System.Threading.Mutex($false, 'Local\FF7R-Codex-Canonical-Http-MCP')
$mutexHeld = $false

function Get-HttpListener {
    try {
        return Get-NetTCPConnection -LocalAddress $BindHost -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -First 1
    } catch {
        $escapedHost = [regex]::Escape($BindHost)
        $pattern = "^\s*TCP\s+$escapedHost`:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
        $line = netstat -ano | Select-String -Pattern $pattern | Select-Object -First 1
        if (-not $line) {
            return $null
        }
        $match = [regex]::Match($line.Line, $pattern)
        if (-not $match.Success) {
            return $null
        }
        return [pscustomobject]@{ LocalAddress = $BindHost; LocalPort = $Port; OwningProcess = [int]$match.Groups[1].Value }
    }
}

function Get-ManagedHttpProcess([int]$ProcessId) {
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process -or -not $process.Path -or $process.Path -notmatch 'python(?:\.exe)?$') {
        return $null
    }
    return $process
}

function Get-ProcessCreationToken($Process) {
    if (-not $Process) {
        return $null
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$Process.CreationDate)) {
        try {
            $created = [System.Management.ManagementDateTimeConverter]::ToDateTime([string]$Process.CreationDate)
            return $created.ToUniversalTime().ToFileTimeUtc().ToString()
        } catch {
            return $null
        }
    }
    try {
        if ($Process.StartTime) {
            return $Process.StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
        }
    } catch {
        return $null
    }
    return $null
}

function Test-HttpCommandIdentity($Process, $Identity) {
    if ([string]$Identity.command_identity -ne 'python -m ff7r_agent.mcp_http_server') {
        return $false
    }
    if ($Process.CommandLine) {
        return $Process.CommandLine -match '(?:^|\s)ff7r_agent\.mcp_http_server(?:\s|$)'
    }
    # Some managed Windows environments deny Win32_Process command-line
    # access. The module writes this exact identity before binding, while the
    # process-creation token still independently rejects PID reuse.
    return $true
}

function Get-RecordedHttpIdentity {
    if (-not (Test-Path -LiteralPath $identityPath -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Get-ValidatedManagedHttpProcess([int]$ProcessId, $Identity) {
    if (-not $Identity -or [int]$Identity.pid -ne $ProcessId) {
        return $null
    }
    if ([string]$Identity.entrypoint -ne 'ff7r_agent.mcp_http_server' -or [string]$Identity.command_identity -ne 'python -m ff7r_agent.mcp_http_server') {
        return $null
    }
    if ([string]$Identity.transport -ne 'streamable-http' -or [string]$Identity.protocol -ne 'ff7r-http-shutdown-v1') {
        return $null
    }
    $process = Get-ManagedHttpProcess $ProcessId
    if (-not $process -or -not (Test-HttpCommandIdentity $process $Identity)) {
        return $null
    }
    $actualToken = Get-ProcessCreationToken $process
    if ([string]::IsNullOrWhiteSpace($actualToken) -or [string]$Identity.process_creation_token -ne $actualToken) {
        return $null
    }
    return $process
}

function Get-RecordedManagedListener {
    if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
        return $null
    }
    $raw = (Get-Content -LiteralPath $pidPath -Raw).Trim()
    $recordedPid = 0
    if (-not [int]::TryParse($raw, [ref]$recordedPid) -or $recordedPid -le 0) {
        return $null
    }
    $listener = Get-HttpListener
    if (-not $listener -or [int]$listener.OwningProcess -ne $recordedPid) {
        return $null
    }
    $process = Get-ValidatedManagedHttpProcess $recordedPid (Get-RecordedHttpIdentity)
    if (-not $process) {
        return $null
    }
    return $process
}

function Test-ProfileIdentity($Identity) {
    if (-not $Identity) {
        return $false
    }
    $actualProfile = if ([string]::IsNullOrWhiteSpace([string]$Identity.deployment_profile)) {
        'CANONICAL'
    } else {
        ([string]$Identity.deployment_profile).ToUpperInvariant()
    }
    if ($actualProfile -ne $profileName) {
        return $false
    }
    if ($profileName -eq 'FEATURE_VALIDATION') {
        if ([string]$Identity.repository_root -ne [string]$repoRoot -or
            [string]$Identity.expected_feature_ref -ne [string]$ExpectedRef -or
            [string]$Identity.expected_feature_commit -ne [string]$ExpectedCommitSha) {
            return $false
        }
    }
    return $true
}

function Invoke-CheckoutValidation {
    $sourceRoot = Join-Path $repoRoot 'Tools\ff7r-codex-agent-mvp'
    $env:PYTHONPATH = Join-Path $sourceRoot 'src'
    $validateArgs = @(
        '-m', 'ff7r_agent.runtime_profile', 'validate-checkout',
        '--profile', $profileName,
        '--repository-root', $repoRoot
    )
    if ($profileName -eq 'FEATURE_VALIDATION') {
        $validateArgs += @('--expected-commit-sha', $ExpectedCommitSha, '--expected-ref', $ExpectedRef)
    } else {
        $validateArgs += @('--expected-branch', $expectedBranch)
    }
    $validationOutput = & $RuntimePython @validateArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "FF7R runtime profile checkout validation failed: $($validationOutput -join ' ')"
    }
    try {
        return ($validationOutput -join "`n" | ConvertFrom-Json)
    } catch {
        throw "FF7R runtime profile checkout validation returned invalid JSON: $($validationOutput -join ' ')"
    }
}

try {
    if (-not (Test-Path -LiteralPath $RuntimePython -PathType Leaf)) {
        throw "Runtime Python not found: $RuntimePython"
    }
    $expectedBranch = $null
    if ($profileName -eq 'CANONICAL') {
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
            throw "Canonical project-state manifest not found: $manifestPath"
        }
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        $expectedBranch = [string]$manifest.canonical_branch
        if ([string]::IsNullOrWhiteSpace($expectedBranch)) {
            throw "Refusing to start FF7R HTTP MCP: canonical_branch is missing from the manifest."
        }
    }
    $checkoutIdentity = Invoke-CheckoutValidation

    try {
        $mutexHeld = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $mutexHeld = $true
    }
    if (-not $mutexHeld) {
        for ($attempt = 0; $attempt -lt 80; $attempt++) {
            $candidate = Get-RecordedManagedListener
            if ($candidate) {
                if (-not (Test-ProfileIdentity (Get-RecordedHttpIdentity))) {
                    throw "A managed FF7R HTTP MCP bridge is already running with a different runtime profile or source binding; stop it through Stop-FF7RHttpMcp.ps1 before replacement."
                }
                Write-Output "FF7R HTTP MCP already running: PID $((Get-Content -LiteralPath $pidPath -Raw).Trim()) ($url)"
                return
            }
            Start-Sleep -Milliseconds 250
        }
        throw 'Another FF7R HTTP MCP launcher owns the singleton lock but did not produce a validated listener.'
    }

    $existing = Get-RecordedManagedListener
    if ($existing) {
        if (-not (Test-ProfileIdentity (Get-RecordedHttpIdentity))) {
            throw "A managed FF7R HTTP MCP bridge is already running with a different runtime profile or source binding; stop it through Stop-FF7RHttpMcp.ps1 before replacement."
        }
        Write-Output "FF7R HTTP MCP already running: PID $existing.ProcessId ($url)"
        return
    }

    $listener = Get-HttpListener
    if ($listener) {
        throw "Port $Port is already in use by an untrusted or unrelated process; refusing to attach."
    }
    if (Test-Path -LiteralPath $pidPath) {
        Remove-Item -LiteralPath $pidPath -Force
    }
    if (Test-Path -LiteralPath $identityPath) {
        Remove-Item -LiteralPath $identityPath -Force
    }

    New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
    $env:PYTHONPATH = Join-Path $repoRoot 'Tools\ff7r-codex-agent-mvp\src'
    $env:FF7R_RUNTIME_PROFILE = $profileName
    $env:FF7R_RUNTIME_REPOSITORY_ROOT = $repoRoot
    $env:FF7R_RUNTIME_SOURCE_ROOT = Join-Path $repoRoot 'Tools\ff7r-codex-agent-mvp'
    $env:FF7R_RUNTIME_PACKAGE_IMPORT_ROOT = Join-Path $repoRoot 'Tools\ff7r-codex-agent-mvp\src'
    $env:FF7R_RUNTIME_EXPECTED_BRANCH = if ($profileName -eq 'CANONICAL') { $expectedBranch } else { '' }
    $env:FF7R_RUNTIME_EXPECTED_COMMIT = if ($profileName -eq 'FEATURE_VALIDATION') { $ExpectedCommitSha } else { '' }
    $env:FF7R_RUNTIME_EXPECTED_REF = if ($profileName -eq 'FEATURE_VALIDATION') { $ExpectedRef } else { '' }
    $env:FF7R_RUNTIME_EXPECTED_CLEAN = 'true'
    $env:FF7R_RUNTIME_DEPLOYMENT_RECORD = $deploymentRecordPath
    $env:FF7R_MCP_HTTP_HOST = $BindHost
    $env:FF7R_MCP_HTTP_PORT = [string]$Port
    $env:FF7R_MCP_HTTP_PATH = '/mcp'
    $env:FF7R_MCP_TRANSPORT = 'streamable-http'
    $env:FF7R_MCP_BRIDGE_NAME = 'ff7r-agent-mcp-http'
    $env:FF7R_MCP_BRIDGE_ENTRYPOINT = 'ff7r_agent.mcp_http_server:main'
    # Telemetry is explicit. Without it the factory uses simulation telemetry.
    if (-not $env:FF7R_INPUT_BACKEND) { $env:FF7R_INPUT_BACKEND = 'simulation' }
    if (-not $env:FF7R_MAX_TELEMETRY_AGE) { $env:FF7R_MAX_TELEMETRY_AGE = '2.0' }

    $process = Start-Process -FilePath $RuntimePython `
        -ArgumentList @('-m', 'ff7r_agent.mcp_http_server') `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru

    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $listener = Get-HttpListener
        if ($listener) {
            $identity = Get-RecordedHttpIdentity
            $managed = Get-ValidatedManagedHttpProcess ([int]$listener.OwningProcess) $identity
            if (-not $managed) {
                if (-not $identity) {
                    Start-Sleep -Milliseconds 100
                    continue
                }
                throw "FF7R HTTP MCP port became occupied by an untrusted or identity-mismatched process during startup."
            }
            if (-not (Test-ProfileIdentity $identity)) {
                throw "FF7R HTTP MCP started but its recorded source profile does not match the requested runtime profile."
            }
            [int]$listener.OwningProcess | Set-Content -LiteralPath $pidPath -Encoding ascii
            $recordArgs = @(
                '-m', 'ff7r_agent.deployment_record',
                '--repository-root', $repoRoot,
                '--output', $deploymentRecordPath,
                '--runtime-python', $RuntimePython,
                '--transport', 'streamable-http',
                '--url', $url,
                '--profile', $profileName
            )
            if ($profileName -eq 'FEATURE_VALIDATION') {
                $recordArgs += @('--expected-ref', $ExpectedRef, '--expected-commit', $ExpectedCommitSha)
            } else {
                $recordArgs += @('--expected-branch', $expectedBranch)
            }
            & $RuntimePython @recordArgs
            if ($LASTEXITCODE -ne 0) {
                throw "FF7R HTTP MCP started but its external deployment record could not be written."
            }
            Write-Output "FF7R HTTP MCP started: PID $($listener.OwningProcess) ($url)"
            return
        }
        if ($process.HasExited) {
            $stderr = if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { '' }
            throw "FF7R HTTP MCP exited during startup. $stderr"
        }
        Start-Sleep -Milliseconds 250
    }

    throw "FF7R HTTP MCP did not begin listening at $url within $StartupTimeoutSeconds seconds."
} finally {
    if ($mutexHeld) {
        try { $mutex.ReleaseMutex() | Out-Null } catch { }
    }
    $mutex.Dispose()
}
