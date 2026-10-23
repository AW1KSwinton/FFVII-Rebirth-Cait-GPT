[CmdletBinding()]
param(
    [string]$BindHost = '127.0.0.1',
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$runtimeDir = if ($env:FF7R_MCP_HTTP_RUNTIME_DIR) { $env:FF7R_MCP_HTTP_RUNTIME_DIR } else { Join-Path $env:LOCALAPPDATA 'ff7r-cait-gpt\mcp-http' }
$pidPath = Join-Path $runtimeDir 'server.pid'
$identityPath = Join-Path $runtimeDir 'server.identity.json'
$tokenPath = Join-Path $runtimeDir 'server.token'
$requestPath = Join-Path $runtimeDir 'shutdown.request'

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
    return $true
}

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

function Get-FF7RDescendantProcesses([int]$RootId) {
    # FF7R runtime processes (worker, rebound bridge, gamepad broker) under the
    # server, each with its creation time so a reused PID is not mistaken for it.
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $found = @()
    $frontier = @($RootId)
    for ($depth = 0; $depth -lt 6 -and $frontier.Count -gt 0; $depth++) {
        $children = @($all | Where-Object { $frontier -contains [int]$_.ParentProcessId })
        $frontier = @($children | ForEach-Object { [int]$_.ProcessId })
        foreach ($child in $children) {
            if ($child.CommandLine -match 'ff7r_agent\.(runtime_worker|runtime_bridge_process|gamepad_broker)') {
                $found += [pscustomobject]@{ ProcessId = [int]$child.ProcessId; Role = $Matches[1]; CreationDate = [string]$child.CreationDate }
            }
        }
    }
    return $found
}

function Test-SameProcess($Recorded) {
    $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($Recorded.ProcessId)" -ErrorAction SilentlyContinue
    return [bool]($current -and [string]$current.CreationDate -eq $Recorded.CreationDate)
}

$descendants = @()
if (-not (Test-Path -LiteralPath $pidPath)) {
    $listener = Get-HttpListener
    if (-not $listener) {
        Write-Output 'FF7R HTTP MCP is not recorded as running.'
        exit 0
    }
    # The launcher can time out before recording server.pid even though the
    # server subsequently binds. Its own identity and shutdown token are
    # written before binding; recover only the listener they identify, then
    # apply every normal process/creation-token/shutdown-token check below.
    try {
        $recordedIdentity = Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json
        $pidValue = [int]$recordedIdentity.pid
    } catch {
        throw "FF7R HTTP MCP is not recorded as running, but port $Port is occupied; refusing to touch an untrusted listener."
    }
    if ($pidValue -le 0 -or $pidValue -ne [int]$listener.OwningProcess -or -not (Get-ManagedHttpProcess $pidValue)) {
        throw "FF7R HTTP MCP is not recorded as running, but port $Port is occupied; refusing to touch an untrusted listener."
    }
} else {
    $pidValue = [int](Get-Content -LiteralPath $pidPath -Raw).Trim()
}

$process = Get-ManagedHttpProcess $pidValue
if ($process) {
    if (-not (Test-Path -LiteralPath $identityPath -PathType Leaf)) {
        throw "Refusing to stop PID $pidValue because its HTTP process-identity record is missing."
    }
    try {
        $identity = Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json
    } catch {
        throw "Refusing to stop PID $pidValue because its HTTP process-identity record is invalid."
    }
    if ([int]$identity.pid -ne $pidValue -or [string]$identity.entrypoint -ne 'ff7r_agent.mcp_http_server' -or [string]$identity.command_identity -ne 'python -m ff7r_agent.mcp_http_server' -or [string]$identity.transport -ne 'streamable-http' -or [string]$identity.protocol -ne 'ff7r-http-shutdown-v1') {
        throw "Refusing to stop PID $pidValue because its HTTP process-identity record does not match the expected entrypoint/protocol."
    }
    if (-not (Test-HttpCommandIdentity $process $identity)) {
        throw "Refusing to stop PID $pidValue because it is not the validated FF7R HTTP MCP process."
    }
    $actualCreationToken = Get-ProcessCreationToken $process
    if ([string]::IsNullOrWhiteSpace($actualCreationToken)) {
        throw "Refusing to stop PID $pidValue because its process creation identity cannot be proven."
    }
    if ([string]::IsNullOrWhiteSpace([string]$identity.process_creation_token) -or [string]$identity.process_creation_token -ne $actualCreationToken) {
        throw "Refusing to stop PID $pidValue because its process creation identity does not match the recorded process."
    }
    if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) {
        throw "Refusing to stop PID $pidValue without the tokenized graceful shutdown control."
    }
    $token = Get-Content -LiteralPath $tokenPath -Raw
    if ([string]::IsNullOrWhiteSpace([string]$token)) {
        throw "Refusing to stop PID $pidValue because its graceful shutdown token is empty."
    }
    $token = $token.Trim()
    # Its runtime workers and gamepad broker exit on their own once it has
    # gone, a few seconds later. A new server started before then refused
    # its first live call (live 2026-10-05: "runtime worker could not become
    # ready"), so they are noted now and awaited below.
    $descendants = @(Get-FF7RDescendantProcesses $pidValue)
    $requestJson = @{ pid = $pidValue; token = $token } | ConvertTo-Json -Compress
    # Windows PowerShell 5.1's -Encoding utf8 writes a BOM. Use explicit
    # BOM-free UTF-8 so the Python watcher receives JSON on every host.
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($requestPath, $requestJson, $utf8NoBom)

    $deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 100
        $remaining = Get-ManagedHttpProcess $pidValue
    } while ($remaining -and (Get-Date) -lt $deadline)

    if ($remaining) {
        # The graceful request has already been validated and given time to
        # neutralise/close RuntimeSupervisor and its children.  This is only
        # a bounded, identity-checked fallback for a wedged HTTP process.
        if (-not (Test-HttpCommandIdentity $remaining $identity)) {
            throw "Refusing fallback termination because PID $pidValue no longer identifies the FF7R HTTP MCP process."
        }
        $remainingCreationToken = Get-ProcessCreationToken $remaining
        if ([string]::IsNullOrWhiteSpace($remainingCreationToken)) {
            throw "Refusing fallback termination because PID $pidValue process identity cannot be proven."
        }
        if ([string]$identity.process_creation_token -ne $remainingCreationToken) {
            throw "Refusing fallback termination because PID $pidValue was reused by a different process."
        }
        Stop-Process -Id $pidValue -Force -ErrorAction Stop
        Wait-Process -Id $pidValue -Timeout 5 -ErrorAction SilentlyContinue
        Write-Output "Graceful FF7R HTTP MCP shutdown timed out; stopped validated fallback PID $pidValue after cleanup request."
    } else {
        Write-Output "Gracefully stopped FF7R HTTP MCP PID $pidValue."
    }
} else {
    Write-Output "FF7R HTTP MCP PID $pidValue was already absent."
}
if (Test-Path -LiteralPath $pidPath) {
    Remove-Item -LiteralPath $pidPath -Force
}
foreach ($path in @($identityPath, $tokenPath, $requestPath, (Join-Path $runtimeDir 'shutdown.complete'))) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
}

$releaseDeadline = (Get-Date).AddSeconds(5)
$released = $false
while ((Get-Date) -lt $releaseDeadline) {
    if (-not (Get-HttpListener)) {
        $released = $true
        break
    }
    Start-Sleep -Milliseconds 100
}
if (-not $released) {
    throw "FF7R HTTP MCP shutdown completed for the validated PID, but port $Port remains occupied; refusing replacement."
}
Write-Output "FF7R HTTP MCP port $Port is released."

$childDeadline = (Get-Date).AddSeconds(20)
do {
    $alive = @($descendants | Where-Object { Test-SameProcess $_ })
    if ($alive.Count -eq 0) { break }
    Start-Sleep -Milliseconds 250
} while ((Get-Date) -lt $childDeadline)
if ($alive.Count -gt 0) {
    $list = ($alive | ForEach-Object { "$($_.ProcessId) ($($_.Role))" }) -join ', '
    throw "FF7R HTTP MCP stopped, but its runtime processes are still running after 20 s: $list; refusing replacement."
}
if ($descendants.Count -gt 0) {
    Write-Output "FF7R HTTP MCP runtime processes have exited ($($descendants.Count))."
}
