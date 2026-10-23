[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$NativeDll,
    [Parameter(Mandatory)][string]$ExpectedDllSha256,
    [string]$GameBinaryRoot = 'D:\Games\FINAL FANTASY VII REBIRTH\End\Binaries\Win64',
    [string]$EvidenceRoot = 'C:\CaitGPT\DisabledRuntimeBackups',
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
if (Get-Process -Name 'ff7rebirth_', 'ff7rebirth' -ErrorAction SilentlyContinue) {
    throw 'FFVII Rebirth is still running; close it before deployment.'
}
if ((Get-FileHash -LiteralPath $NativeDll -Algorithm SHA256).Hash -ne $ExpectedDllSha256) {
    throw 'Native DLL does not match the explicitly selected build.'
}
$ue4ssPath = Join-Path $GameBinaryRoot 'ue4ss\UE4SS.dll'
if ((Get-FileHash -LiteralPath $ue4ssPath -Algorithm SHA256).Hash -ne '3F5E8D934FE91FF26BE2CE3585CEF0940D3C73B40295ADC09FA6E7B62BC0018F') {
    throw 'This sampler requires the verified c0a8d4c UE4SS runtime; installation refused.'
}
$modsRoot = Join-Path $GameBinaryRoot 'ue4ss\Mods'
$modsFile = Join-Path $modsRoot 'mods.txt'
$modsText = Get-Content -LiteralPath $modsFile -Raw
$probeRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'ff7r-navigation-probe\Scripts'
$changes = @(
    @{ source=$NativeDll; relative='FF7RTelemetryExporter\dlls\main.dll' },
    @{ source=(Join-Path $PSScriptRoot 'Base\Scripts\main.lua'); relative='FF7RTelemetryExporter\scripts\main.lua' },
    @{ source=(Join-Path $PSScriptRoot 'Base\Scripts\entity_poi.lua'); relative='FF7RTelemetryExporter\scripts\entity_poi.lua' },
    @{ source=(Join-Path $PSScriptRoot 'Base\Scripts\map_query.lua'); relative='FF7RTelemetryExporter\scripts\map_query.lua' },
    @{ source=(Join-Path $PSScriptRoot 'Base\Scripts\route_sweep.lua'); relative='FF7RTelemetryExporter\scripts\route_sweep.lua' },
    @{ source=(Join-Path $PSScriptRoot 'Base\Scripts\cutscene.lua'); relative='FF7RTelemetryExporter\scripts\cutscene.lua' },
    @{ source=(Join-Path $PSScriptRoot 'Base\Scripts\objective_query.lua'); relative='FF7RTelemetryExporter\scripts\objective_query.lua' },
    @{ source=(Join-Path $PSScriptRoot 'Base\Scripts\combat_query.lua'); relative='FF7RTelemetryExporter\scripts\combat_query.lua' },
    @{ source=(Join-Path $PSScriptRoot 'Shared\Scripts\object_refs.lua'); relative='FF7RTelemetryExporter\scripts\object_refs.lua' },
    @{ source=(Join-Path $probeRoot 'main.lua'); relative='FF7RNavigationProbe\scripts\main.lua' },
    @{ source=(Join-Path $PSScriptRoot 'Shared\Scripts\object_refs.lua'); relative='FF7RNavigationProbe\scripts\object_refs.lua' },
    @{ source=(Join-Path $probeRoot 'navmesh_path.lua'); relative='FF7RNavmeshPathProbe\scripts\main.lua' },
    @{ source=(Join-Path $PSScriptRoot 'Shared\Scripts\object_refs.lua'); relative='FF7RNavmeshPathProbe\scripts\object_refs.lua' },
    @{ source=(Join-Path $probeRoot 'custom_nav_api_probe.lua'); relative='FF7RCustomNavAPIProbe\scripts\main.lua' }
)
$modNames = @($changes | ForEach-Object { $_.relative.Split('\')[0] } | Select-Object -Unique)
foreach ($modName in $modNames) {
    if ($modsText -notmatch ('(?m)^' + [regex]::Escape($modName) + '\s*:\s*1\s*$')) {
        throw "Required sampler is not enabled in the existing installation: $modName"
    }
}
foreach ($change in $changes) {
    $change.destination = Join-Path $modsRoot $change.relative
    $change.before_sha256 = if (Test-Path -LiteralPath $change.destination) { (Get-FileHash -LiteralPath $change.destination -Algorithm SHA256).Hash } else { $null }
    $change.source_sha256 = (Get-FileHash -LiteralPath $change.source -Algorithm SHA256).Hash
}
if (-not $Apply) {
    [pscustomobject]@{ dry_run=$true; changes=$changes; backup_verified=$false }
    return
}
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmssZ')
$backupRoot = Join-Path $EvidenceRoot ('PersistentSamplers-before-' + $stamp + '-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $backupRoot | Out-Null
$originals = @()
foreach ($modName in $modNames) {
    $sourceDirectory = Join-Path $modsRoot $modName
    foreach ($file in Get-ChildItem -LiteralPath $sourceDirectory -File -Recurse) {
        $relative = $file.FullName.Substring($modsRoot.Length + 1)
        $originals += @{ relative=$relative; sha256=(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash }
    }
    Copy-Item -LiteralPath $sourceDirectory -Destination (Join-Path $backupRoot $modName) -Recurse
}
$originals += @{ relative='mods.txt'; sha256=(Get-FileHash -LiteralPath $modsFile -Algorithm SHA256).Hash }
Copy-Item -LiteralPath $modsFile -Destination (Join-Path $backupRoot 'mods.txt')
foreach ($original in $originals) {
    if ((Get-FileHash -LiteralPath (Join-Path $backupRoot $original.relative) -Algorithm SHA256).Hash -ne $original.sha256) {
        throw "Complete backup verification failed: $($original.relative)"
    }
}
$record = @{ backup=$backupRoot; backup_verified=$true; originals=$originals; changes=$changes; installed=$false }
$recordPath = Join-Path $backupRoot 'deployment.json'
$record | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $recordPath -Encoding UTF8
$staged = @()
try {
    # Stage and verify the complete paired set before replacing the first file.
    foreach ($change in $changes) {
        $change.staged = $change.destination + '.deploying.' + [guid]::NewGuid().ToString('N')
        $staged += $change.staged
        Copy-Item -LiteralPath $change.source -Destination $change.staged
        if ((Get-FileHash -LiteralPath $change.staged -Algorithm SHA256).Hash -ne $change.source_sha256) {
            throw 'Staged source hash mismatch.'
        }
    }
    if (Get-Process -Name 'ff7rebirth_', 'ff7rebirth' -ErrorAction SilentlyContinue) {
        throw 'Game appeared during staging; installation refused.'
    }
    foreach ($change in $changes) {
        $actualBefore = if (Test-Path -LiteralPath $change.destination) { (Get-FileHash -LiteralPath $change.destination -Algorithm SHA256).Hash } else { $null }
        if ($actualBefore -ne $change.before_sha256) {
            throw 'Installed files changed since backup; installation refused.'
        }
    }
    foreach ($change in $changes) {
        Move-Item -LiteralPath $change.staged -Destination $change.destination -Force
        if ((Get-FileHash -LiteralPath $change.destination -Algorithm SHA256).Hash -ne $change.source_sha256) {
            throw 'Installed source hash mismatch; verified backup is retained.'
        }
    }
    $record.installed = $true
}
finally {
    foreach ($path in $staged) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
    }
    $record | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $recordPath -Encoding UTF8
}
[pscustomobject]$record
