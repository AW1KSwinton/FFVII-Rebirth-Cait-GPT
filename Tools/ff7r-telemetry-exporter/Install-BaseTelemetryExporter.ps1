param(
    [string]$GameBinaryRoot = 'D:\Games\FINAL FANTASY VII REBIRTH\End\Binaries\Win64',
    [string]$EvidenceRoot = 'C:\CaitGPT\DisabledRuntimeBackups',
    [string]$NativeDll = '',
    [string]$ExpectedDllSha256 = ''
)

$ErrorActionPreference = 'Stop'
$modName = 'FF7RTelemetryExporter'
$modsRoot = Join-Path (Join-Path $GameBinaryRoot 'ue4ss') 'Mods'
$modsFile = Join-Path $modsRoot 'mods.txt'
$sourceScript = Join-Path $PSScriptRoot 'Base\Scripts\main.lua'
$destinationDir = Join-Path $modsRoot $modName
$destinationScript = Join-Path $destinationDir 'Scripts\main.lua'

# Persistent samplers require the complete paired DLL/script set.
if ((Get-Content -LiteralPath $sourceScript -Raw) -match 'function FF7RReadOnlyTick') {
    if (-not $NativeDll -or -not $ExpectedDllSha256) {
        throw 'Use Install-PersistentSamplers.ps1 with a verified native DLL; Lua-only installation is refused.'
    }
    & (Join-Path $PSScriptRoot 'Install-PersistentSamplers.ps1') -NativeDll $NativeDll -ExpectedDllSha256 $ExpectedDllSha256 -GameBinaryRoot $GameBinaryRoot -EvidenceRoot $EvidenceRoot -Apply
    return
}

if (-not (Test-Path -LiteralPath $sourceScript -PathType Leaf)) {
    throw "Base telemetry source was not found: $sourceScript"
}
if (-not (Test-Path -LiteralPath $modsFile -PathType Leaf)) {
    throw "UE4SS mods.txt was not found: $modsFile"
}
if (-not (Test-Path -LiteralPath $destinationDir -PathType Container) -or
    -not (Test-Path -LiteralPath $destinationScript -PathType Leaf)) {
    throw "The existing $modName installation was not found under: $destinationDir"
}

$running = @(Get-Process -Name 'ff7rebirth_', 'ff7rebirth' -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    throw 'FFVII Rebirth is still running; close it before deployment.'
}

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmssZ')
$backupRoot = Join-Path $EvidenceRoot ("TelemetryExporter-before-" + $stamp)
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
Copy-Item -LiteralPath $modsFile -Destination (Join-Path $backupRoot 'mods.txt') -Force
Copy-Item -LiteralPath $destinationDir -Destination (Join-Path $backupRoot $modName) -Recurse -Force

$sourceHash = (Get-FileHash -LiteralPath $sourceScript -Algorithm SHA256).Hash
$beforeHash = (Get-FileHash -LiteralPath $destinationScript -Algorithm SHA256).Hash
$temporaryScript = Join-Path $destinationDir ('.main.lua.deploying.' + [guid]::NewGuid().ToString('N'))
try {
    Copy-Item -LiteralPath $sourceScript -Destination $temporaryScript -Force
    $stagedHash = (Get-FileHash -LiteralPath $temporaryScript -Algorithm SHA256).Hash
    if ($stagedHash -ne $sourceHash) {
        throw 'Staged base telemetry script hash did not match the repository source.'
    }
    Move-Item -LiteralPath $temporaryScript -Destination $destinationScript -Force

    $modsText = Get-Content -LiteralPath $modsFile -Raw
    $entryPattern = '(?m)^' + [regex]::Escape($modName) + '\s*:\s*\d+\s*$'
    if ($modsText -match $entryPattern) {
        $modsText = [regex]::Replace($modsText, $entryPattern, "$modName : 1")
    }
    else {
        $modsText = $modsText.TrimEnd() + "`r`n$modName : 1`r`n"
    }
    [System.IO.File]::WriteAllText(
        $modsFile,
        $modsText,
        (New-Object System.Text.UTF8Encoding($false))
    )
}
finally {
    if (Test-Path -LiteralPath $temporaryScript) {
        Remove-Item -LiteralPath $temporaryScript -Force
    }
}

$afterHash = (Get-FileHash -LiteralPath $destinationScript -Algorithm SHA256).Hash
if ($afterHash -ne $sourceHash) {
    throw "Installed base telemetry script hash did not match the repository source: $afterHash"
}

[pscustomobject]@{
    installed = $true
    mod = $modName
    source = $sourceScript
    source_sha256 = $sourceHash
    destination = $destinationScript
    before_sha256 = $beforeHash
    installed_sha256 = $afterHash
    backup = $backupRoot
}
