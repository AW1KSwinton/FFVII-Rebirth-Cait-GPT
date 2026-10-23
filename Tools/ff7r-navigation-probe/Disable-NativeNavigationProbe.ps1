param(
    [string]$GameBinaryRoot = 'D:\Games\FINAL FANTASY VII REBIRTH\End\Binaries\Win64',
    [string]$EvidenceRoot = 'C:\CaitGPT\DisabledRuntimeBackups'
)

$ErrorActionPreference = 'Stop'
$probeName = 'FF7RNavigationNativeProbe'
$modsFile = Join-Path (Join-Path $GameBinaryRoot 'ue4ss') 'Mods\mods.txt'
$queryMarker = 'C:\CaitGPT\Telemetry\navigation-native-query.enable'
$geometryMarker = 'C:\CaitGPT\Telemetry\navigation-native-geometry.enable'
$geometryReadyMarker = 'C:\CaitGPT\Telemetry\navigation-native-geometry.ready'
if (-not (Test-Path -LiteralPath $modsFile -PathType Leaf)) { throw "UE4SS mods.txt was not found: $modsFile" }
if (@(Get-Process -Name 'ff7rebirth_', 'ff7rebirth' -ErrorAction SilentlyContinue).Count -gt 0) { throw 'Disable the native probe during a maintenance window while Rebirth is closed.' }
if (Test-Path -LiteralPath $queryMarker) { Remove-Item -LiteralPath $queryMarker -Force }
if (Test-Path -LiteralPath $geometryMarker) { Remove-Item -LiteralPath $geometryMarker -Force }
if (Test-Path -LiteralPath $geometryReadyMarker) { Remove-Item -LiteralPath $geometryReadyMarker -Force }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $EvidenceRoot ("NativeNavigationProbe-disable-before-" + $stamp)
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
Copy-Item -LiteralPath $modsFile -Destination (Join-Path $backupRoot 'mods.txt') -Force
$modsText = Get-Content -LiteralPath $modsFile -Raw
$entryPattern = '(?m)^' + [regex]::Escape($probeName) + '\s*:\s*\d+\s*$'
if ($modsText -match $entryPattern) {
    $modsText = [regex]::Replace($modsText, $entryPattern, "$probeName : 0")
    [System.IO.File]::WriteAllText($modsFile, $modsText, (New-Object System.Text.UTF8Encoding($false)))
}
[pscustomobject]@{ disabled = $true; probe = $probeName; mods = $modsFile; backup = $backupRoot; query_marker_removed = $true; geometry_marker_removed = $true; geometry_readiness_marker_removed = $true }
