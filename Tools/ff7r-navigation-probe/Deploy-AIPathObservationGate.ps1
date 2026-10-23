[CmdletBinding()]
param(
    [string]$EvidenceRoot = 'C:\CaitGPT\DisabledRuntimeBackups',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'

# This helper changes exactly one installed callback. Keep these pins explicit
# so it refuses an unexpected local edit or a different installed baseline.
$expectedSourceSha256 = '09959273035C53B655D92A16751D9270842280D5A2435A47B54BDC67F86846EC'
$expectedInstalledSha256 = '749B6F1670811B3E5F54969FA7799DCDEC495C521ADFEB645041FB42EDA6104F'
$sourcePath = Join-Path $PSScriptRoot 'Scripts\navmesh_path.lua'
$gameBinaryRoot = 'D:\Games\FINAL FANTASY VII REBIRTH\End\Binaries\Win64'
$destinationPath = Join-Path $gameBinaryRoot 'ue4ss\Mods\FF7RNavmeshPathProbe\scripts\main.lua'
$objectRefsSource = Join-Path (Split-Path -Parent $PSScriptRoot) 'ff7r-telemetry-exporter\Shared\Scripts\object_refs.lua'
$objectRefsInstalled = Join-Path (Split-Path -Parent $destinationPath) 'object_refs.lua'

function Get-RunningGameOrLauncher {
    return @(Get-Process -Name 'ff7rebirth_', 'ff7rebirth' -ErrorAction SilentlyContinue)
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
    throw "Repo source is missing: $sourcePath"
}
if (-not (Test-Path -LiteralPath $destinationPath -PathType Leaf)) {
    throw "Installed callback is missing: $destinationPath"
}

$sourceSha256 = Get-Sha256 $sourcePath
$installedSha256 = Get-Sha256 $destinationPath
$running = Get-RunningGameOrLauncher
$sourceMatches = $sourceSha256 -eq $expectedSourceSha256
$installedMatches = $installedSha256 -eq $expectedInstalledSha256
$objectRefsHash = Get-Sha256 $objectRefsSource
$objectRefsMatches = (Test-Path -LiteralPath $objectRefsInstalled -PathType Leaf) -and
    ((Get-Sha256 $objectRefsInstalled) -eq $objectRefsHash)
$processesStopped = $running.Count -eq 0

if (-not $Apply) {
    # The default dry run is read-only, including when Rebirth or its launcher
    # is running. It creates no backup, staging file, marker, or log.
    [pscustomobject]@{
        dry_run = $true
        writes_performed = $false
        apply_ready = $sourceMatches -and $installedMatches -and $objectRefsMatches -and $processesStopped
        object_refs_ready = $objectRefsMatches
        object_refs_sha256 = $objectRefsHash
        source_path = $sourcePath
        source_sha256 = $sourceSha256
        expected_source_sha256 = $expectedSourceSha256
        destination_path = $destinationPath
        installed_sha256 = $installedSha256
        expected_installed_sha256 = $expectedInstalledSha256
        running_game_or_launcher = @($running | ForEach-Object { @{ pid = $_.Id; name = $_.ProcessName } })
    }
    return
}

if (-not $sourceMatches) { throw "Repo source hash mismatch: $sourceSha256" }
if (-not $installedMatches) { throw "Installed callback hash mismatch: $installedSha256" }
if (-not $objectRefsMatches) {
    throw 'Fresh object lookup module is missing or differs; use Install-PersistentSamplers.ps1 for the paired deployment.'
}
if (-not $processesStopped) {
    $names = ($running | ForEach-Object { "$($_.ProcessName) (PID $($_.Id))" }) -join ', '
    throw "FFVII Rebirth and its launcher must both be closed before deployment: $names"
}

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmssZ')
$backupRoot = Join-Path $EvidenceRoot ("AIPathObservationGate-before-{0}-{1}" -f $stamp, [guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $backupRoot) { throw "Unique backup directory already exists: $backupRoot" }
if (-not (Test-Path -LiteralPath $backupRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
}
$backupPath = Join-Path $backupRoot 'main.lua'

$stagedPath = $destinationPath + '.deploying.' + [guid]::NewGuid().ToString('N')
$restorePath = $destinationPath + '.restoring.' + [guid]::NewGuid().ToString('N')
$backupVerified = $false
$replacementAttempted = $false
$recordPath = Join-Path $backupRoot 'deployment.json'
$record = [ordered]@{
    operation = 'single_file_ai_path_observation_gate'
    source = $sourcePath
    destination = $destinationPath
    source_sha256 = $sourceSha256
    installed_sha256_before = $installedSha256
    installed_sha256_after = $null
    backup = $backupPath
    backup_sha256 = $null
    backup_verified = $false
    installed = $false
    restored_after_failure = $false
    timestamp_utc = (Get-Date).ToUniversalTime().ToString('o')
}

try {
    Copy-Item -LiteralPath $destinationPath -Destination $backupPath
    $record.backup_sha256 = (Get-Sha256 $backupPath)
    if ($record.backup_sha256 -ne $expectedInstalledSha256) {
        throw "Backup verification failed: $($record.backup_sha256)"
    }
    $backupVerified = $true
    $record.backup_verified = $true
    $record | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $recordPath -Encoding UTF8

    Copy-Item -LiteralPath $sourcePath -Destination $stagedPath
    if ((Get-Sha256 $stagedPath) -ne $expectedSourceSha256) {
        throw 'Staged source failed SHA-256 verification.'
    }
    if ((Get-Sha256 $destinationPath) -ne $expectedInstalledSha256) {
        throw 'Installed callback changed after preflight; refusing replacement.'
    }
    if ((Get-Sha256 $objectRefsInstalled) -ne $objectRefsHash) {
        throw 'Fresh object lookup module changed after preflight; refusing replacement.'
    }
    $running = Get-RunningGameOrLauncher
    if ($running.Count -gt 0) {
        throw 'FFVII Rebirth or its launcher started during deployment; refusing replacement.'
    }

    $replacementAttempted = $true
    # Stage beside the destination, then atomically replace only main.lua.
    [System.IO.File]::Replace($stagedPath, $destinationPath, $null)
    $record.installed_sha256_after = (Get-Sha256 $destinationPath)
    if ($record.installed_sha256_after -ne $expectedSourceSha256) {
        throw "Installed callback failed post-replacement verification: $($record.installed_sha256_after)"
    }
    $record.installed = $true
    $record | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $recordPath -Encoding UTF8

    [pscustomobject]@{
        dry_run = $false
        installed = $true
        destination_path = $destinationPath
        installed_sha256 = $record.installed_sha256_after
        backup_path = $backupPath
        manifest_path = $recordPath
        backup_sha256 = $record.backup_sha256
        backup_verified = $true
    }
}
catch {
    $failure = $_
    if ($replacementAttempted -and $backupVerified) {
        try {
            $running = Get-RunningGameOrLauncher
            if ($running.Count -gt 0) {
                $names = ($running | ForEach-Object { "$($_.ProcessName) (PID $($_.Id))" }) -join ', '
                throw "Cannot safely restore while Rebirth or its launcher is running: $names. Preserve backup: $backupPath"
            }
            if ((Get-Sha256 $backupPath) -ne $expectedInstalledSha256) {
                throw "Verified backup no longer matches the original SHA-256: $backupPath"
            }
            $currentHash = $null
            if (Test-Path -LiteralPath $destinationPath -PathType Leaf) {
                try { $currentHash = Get-Sha256 $destinationPath } catch { $currentHash = $null }
            }
            if ($currentHash -ne $expectedInstalledSha256) {
                Copy-Item -LiteralPath $backupPath -Destination $restorePath
                if ((Get-Sha256 $restorePath) -ne $expectedInstalledSha256) {
                    throw 'Verified backup copy failed before restore.'
                }
                if (Test-Path -LiteralPath $destinationPath -PathType Leaf) {
                    [System.IO.File]::Replace($restorePath, $destinationPath, $null)
                } else {
                    [System.IO.File]::Move($restorePath, $destinationPath)
                }
                if ((Get-Sha256 $destinationPath) -ne $expectedInstalledSha256) {
                    throw 'Restored callback does not match the original SHA-256.'
                }
                $record.restored_after_failure = $true
            }
        }
        catch {
            throw "Deployment failed ($failure); verified restore also failed ($_). Preserve backup: $backupPath; manifest: $recordPath"
        }
    }
    $record.installed_sha256_after = if (Test-Path -LiteralPath $destinationPath -PathType Leaf) { Get-Sha256 $destinationPath } else { $null }
    $record | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $recordPath -Encoding UTF8
    throw "Deployment failed; installed callback is $($record.installed_sha256_after). Verified backup: $backupPath; manifest: $recordPath. Cause: $failure"
}
finally {
    foreach ($temporaryPath in @($stagedPath, $restorePath)) {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
}
