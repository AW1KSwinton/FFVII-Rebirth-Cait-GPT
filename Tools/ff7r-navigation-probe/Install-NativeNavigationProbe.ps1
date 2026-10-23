param(
    [string]$GameBinaryRoot = 'D:\Games\FINAL FANTASY VII REBIRTH\End\Binaries\Win64',
    [string]$EvidenceRoot = 'C:\CaitGPT\DisabledRuntimeBackups',
    [string]$BuildDirectory = (Join-Path $PSScriptRoot 'Native\Build')
)

$ErrorActionPreference = 'Stop'
$probeName = 'FF7RNavigationNativeProbe'
$modsRoot = Join-Path (Join-Path $GameBinaryRoot 'ue4ss') 'Mods'
$modsFile = Join-Path $modsRoot 'mods.txt'
$queryMarker = 'C:\CaitGPT\Telemetry\navigation-native-query.enable'
$geometryMarker = 'C:\CaitGPT\Telemetry\navigation-native-geometry.enable'
$geometryReadyMarker = 'C:\CaitGPT\Telemetry\navigation-native-geometry.ready'
$sourceDll = Join-Path $BuildDirectory 'Release\main.dll'
$destinationDir = Join-Path $modsRoot $probeName
$destinationDll = Join-Path $destinationDir 'dlls\main.dll'

if (-not (Test-Path -LiteralPath $sourceDll -PathType Leaf)) { throw "Native probe DLL was not found: $sourceDll" }
if (-not (Test-Path -LiteralPath $modsFile -PathType Leaf)) { throw "UE4SS mods.txt was not found: $modsFile" }
if (@(Get-Process -Name 'ff7rebirth_', 'ff7rebirth' -ErrorAction SilentlyContinue).Count -gt 0) { throw 'FFVII Rebirth is still running; close it before deployment.' }
if (Test-Path -LiteralPath $queryMarker) { Remove-Item -LiteralPath $queryMarker -Force }
if (Test-Path -LiteralPath $geometryMarker) { Remove-Item -LiteralPath $geometryMarker -Force }
if (Test-Path -LiteralPath $geometryReadyMarker) { Remove-Item -LiteralPath $geometryReadyMarker -Force }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $EvidenceRoot ("NativeNavigationProbe-before-" + $stamp)
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
Copy-Item -LiteralPath $modsFile -Destination (Join-Path $backupRoot 'mods.txt') -Force
if (Test-Path -LiteralPath $destinationDir) { Copy-Item -LiteralPath $destinationDir -Destination (Join-Path $backupRoot $probeName) -Recurse -Force }
New-Item -ItemType Directory -Path (Split-Path -Parent $destinationDll) -Force | Out-Null
Copy-Item -LiteralPath $sourceDll -Destination $destinationDll -Force

$modsText = Get-Content -LiteralPath $modsFile -Raw
$entryPattern = '(?m)^' + [regex]::Escape($probeName) + '\s*:\s*\d+\s*$'
if ($modsText -match $entryPattern) { $modsText = [regex]::Replace($modsText, $entryPattern, "$probeName : 1") } else { $modsText = $modsText.TrimEnd() + "`r`n$probeName : 1`r`n" }
[System.IO.File]::WriteAllText($modsFile, $modsText, (New-Object System.Text.UTF8Encoding($false)))

[pscustomobject]@{
    installed = $true
    probe = $probeName
    dll = $destinationDll
    dll_sha256 = (Get-FileHash -LiteralPath $destinationDll -Algorithm SHA256).Hash
    backup = $backupRoot
    native_query_opt_in = $false
    stale_query_marker_removed = $true
    stale_geometry_marker_removed = $true
    stale_geometry_readiness_marker_removed = $true
    movement_sent = $false
}
