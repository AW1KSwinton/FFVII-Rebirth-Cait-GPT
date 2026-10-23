param(
    [string]$GameRoot = 'D:\Games\FINAL FANTASY VII REBIRTH\End\Binaries\Win64'
)

$ErrorActionPreference = 'Stop'
$game = Get-Process -Name 'ff7rebirth_', 'ff7rebirth' -ErrorAction SilentlyContinue
if ($game) {
    throw 'FFVII Rebirth is still running; close it before disabling the probe.'
}

$modsPath = Join-Path $GameRoot 'ue4ss\Mods\mods.txt'
if (-not (Test-Path -LiteralPath $modsPath -PathType Leaf)) {
    throw "UE4SS mods.txt was not found: $modsPath"
}

$backupRoot = 'C:\CaitGPT\DisabledRuntimeBackups'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
$backup = Join-Path $backupRoot "CustomAIStateProbe-disabled-$stamp-mods.txt"
Copy-Item -LiteralPath $modsPath -Destination $backup -Force

$lines = Get-Content -LiteralPath $modsPath
$found = $false
$updated = foreach ($line in $lines) {
    if ($line -match '^\s*FF7RCustomAIStateProbe\s*:') {
        $found = $true
        'FF7RCustomAIStateProbe : 0'
    } else {
        $line
    }
}
if (-not $found) {
    $updated += 'FF7RCustomAIStateProbe : 0'
}
Set-Content -LiteralPath $modsPath -Value $updated -Encoding UTF8

[pscustomobject]@{
    disabled = $true
    probe = 'FF7RCustomAIStateProbe'
    mods = $modsPath
    backup = $backup
}
