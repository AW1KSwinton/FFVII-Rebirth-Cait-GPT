param(
    [string]$GameBinaryRoot = 'D:\Games\FINAL FANTASY VII REBIRTH\End\Binaries\Win64',
    [string]$EvidenceRoot = 'C:\CaitGPT\DisabledRuntimeBackups'
)
$ErrorActionPreference = 'Stop'
$probeName = 'FF7RMapSurfaceProbe'
$modsFile = Join-Path (Join-Path $GameBinaryRoot 'ue4ss') 'Mods\mods.txt'
if (-not (Test-Path -LiteralPath $modsFile -PathType Leaf)) { throw "UE4SS mods.txt was not found: $modsFile" }
$running = @(Get-Process -Name 'ff7rebirth_', 'ff7rebirth' -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) { throw 'FFVII Rebirth is still running; disable the probe during a maintenance window.' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $EvidenceRoot ("MapSurfaceProbe-disable-before-" + $stamp)
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
Copy-Item -LiteralPath $modsFile -Destination (Join-Path $backupRoot 'mods.txt') -Force
$modsText = Get-Content -LiteralPath $modsFile -Raw
$entryPattern = '(?m)^' + [regex]::Escape($probeName) + '\s*:\s*\d+\s*$'
if ($modsText -match $entryPattern) {
    $modsText = [regex]::Replace($modsText, $entryPattern, "$probeName : 0")
    [System.IO.File]::WriteAllText($modsFile, $modsText, (New-Object System.Text.UTF8Encoding($false)))
}
[pscustomobject]@{ disabled = $true; probe = $probeName; mods = $modsFile; backup = $backupRoot }
