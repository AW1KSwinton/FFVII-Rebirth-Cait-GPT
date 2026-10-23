param(
    [string]$GameBinaryRoot = 'D:\Games\FINAL FANTASY VII REBIRTH\End\Binaries\Win64',
    [string]$EvidenceRoot = 'C:\CaitGPT\DisabledRuntimeBackups'
)

$ErrorActionPreference = 'Stop'
$probeName = 'FF7RCustomAIStateProbe'
$modsRoot = Join-Path (Join-Path $GameBinaryRoot 'ue4ss') 'Mods'
$modsFile = Join-Path $modsRoot 'mods.txt'
$sourceScript = Join-Path $PSScriptRoot 'Scripts\custom_ai_state.lua'
$destinationDir = Join-Path $modsRoot $probeName
$destinationScript = Join-Path $destinationDir 'Scripts\main.lua'
if (-not (Test-Path -LiteralPath $sourceScript -PathType Leaf)) { throw "Custom AI state probe source was not found: $sourceScript" }
if (-not (Test-Path -LiteralPath $modsFile -PathType Leaf)) { throw "UE4SS mods.txt was not found: $modsFile" }
$running = @(Get-Process -Name 'ff7rebirth_', 'ff7rebirth' -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) { throw 'FFVII Rebirth is still running; close it before deployment.' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $EvidenceRoot ("CustomAIStateProbe-before-" + $stamp)
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
Copy-Item -LiteralPath $modsFile -Destination (Join-Path $backupRoot 'mods.txt') -Force
if (Test-Path -LiteralPath $destinationDir) { Copy-Item -LiteralPath $destinationDir -Destination (Join-Path $backupRoot $probeName) -Recurse -Force }
New-Item -ItemType Directory -Path (Split-Path -Parent $destinationScript) -Force | Out-Null
Copy-Item -LiteralPath $sourceScript -Destination $destinationScript -Force
$modsText = Get-Content -LiteralPath $modsFile -Raw
$entryPattern = '(?m)^' + [regex]::Escape($probeName) + '\s*:\s*\d+\s*$'
if ($modsText -match $entryPattern) { $modsText = [regex]::Replace($modsText, $entryPattern, "$probeName : 1") } else { $modsText = $modsText.TrimEnd() + "`r`n$probeName : 1`r`n" }
[System.IO.File]::WriteAllText($modsFile, $modsText, (New-Object System.Text.UTF8Encoding($false)))
[pscustomobject]@{ installed = $true; probe = $probeName; script = $destinationScript; script_sha256 = (Get-FileHash -LiteralPath $destinationScript -Algorithm SHA256).Hash; backup = $backupRoot }
