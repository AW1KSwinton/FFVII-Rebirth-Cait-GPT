param(
    [string]$GameBinaryRoot = 'D:\Games\FINAL FANTASY VII REBIRTH\End\Binaries\Win64',
    [string]$EvidenceRoot = 'C:\CaitGPT\DisabledRuntimeBackups'
)

$ErrorActionPreference = 'Stop'
$probeName = 'FF7RQueenBloodProbe'
$modsRoot = Join-Path (Join-Path $GameBinaryRoot 'ue4ss') 'Mods'
$modsFile = Join-Path $modsRoot 'mods.txt'
$sourceScript = Join-Path $PSScriptRoot 'Scripts\main.lua'
$sourceCatalogue = Join-Path $PSScriptRoot 'Scripts\card_catalogue.lua'
$sourceEnumerator = Join-Path $PSScriptRoot 'Scripts\card_enumerator.lua'
$sourceObjectRefs = Join-Path (Split-Path -Parent $PSScriptRoot) 'Shared\Scripts\object_refs.lua'
$destinationDir = Join-Path $modsRoot $probeName
$destinationScript = Join-Path $destinationDir 'Scripts\main.lua'
$destinationCatalogue = Join-Path $destinationDir 'Scripts\card_catalogue.lua'
$destinationEnumerator = Join-Path $destinationDir 'Scripts\card_enumerator.lua'
$destinationObjectRefs = Join-Path $destinationDir 'Scripts\object_refs.lua'

if (-not (Test-Path -LiteralPath $sourceScript -PathType Leaf)) {
    throw "Queen's Blood probe source was not found: $sourceScript"
}
if (-not (Test-Path -LiteralPath $sourceCatalogue -PathType Leaf)) {
    throw "Queen's Blood card catalogue was not found: $sourceCatalogue"
}
if (-not (Test-Path -LiteralPath $sourceEnumerator -PathType Leaf)) {
    throw "Queen's Blood card enumerator was not found: $sourceEnumerator"
}
if (-not (Test-Path -LiteralPath $sourceObjectRefs -PathType Leaf)) {
    throw "Fresh object lookup module was not found: $sourceObjectRefs"
}
if (-not (Test-Path -LiteralPath $modsFile -PathType Leaf)) {
    throw "UE4SS mods.txt was not found: $modsFile"
}
if (@(Get-Process -Name 'ff7rebirth_', 'ff7rebirth' -ErrorAction SilentlyContinue).Count -gt 0) {
    throw 'FFVII Rebirth is still running; close it before deployment.'
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $EvidenceRoot ("QueenBloodProbe-before-" + $stamp)
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
Copy-Item -LiteralPath $modsFile -Destination (Join-Path $backupRoot 'mods.txt') -Force
if (Test-Path -LiteralPath $destinationDir) {
    Copy-Item -LiteralPath $destinationDir -Destination (Join-Path $backupRoot $probeName) -Recurse -Force
}
# Verify every preserved file before replacing any installed source.
if ((Get-FileHash -LiteralPath $modsFile).Hash -ne
    (Get-FileHash -LiteralPath (Join-Path $backupRoot 'mods.txt')).Hash) {
    throw 'The mods.txt backup did not verify; deployment stopped.'
}
if (Test-Path -LiteralPath $destinationDir) {
    foreach ($file in Get-ChildItem -LiteralPath $destinationDir -Recurse -File) {
        $relative = $file.FullName.Substring($destinationDir.Length).TrimStart('\')
        $backupFile = Join-Path (Join-Path $backupRoot $probeName) $relative
        if ((Get-FileHash -LiteralPath $file.FullName).Hash -ne
            (Get-FileHash -LiteralPath $backupFile).Hash) {
            throw "The probe backup did not verify: $relative; deployment stopped."
        }
    }
}

New-Item -ItemType Directory -Path (Split-Path -Parent $destinationScript) -Force | Out-Null
Copy-Item -LiteralPath $sourceScript -Destination $destinationScript -Force
Copy-Item -LiteralPath $sourceCatalogue -Destination $destinationCatalogue -Force
Copy-Item -LiteralPath $sourceEnumerator -Destination $destinationEnumerator -Force
Copy-Item -LiteralPath $sourceObjectRefs -Destination $destinationObjectRefs -Force
$sourceHash = (Get-FileHash -LiteralPath $sourceScript -Algorithm SHA256).Hash
$installedHash = (Get-FileHash -LiteralPath $destinationScript -Algorithm SHA256).Hash
if ($sourceHash -ne $installedHash) {
    throw "Installed Queen's Blood probe hash does not match the repository source."
}
$catalogueHash = (Get-FileHash -LiteralPath $sourceCatalogue -Algorithm SHA256).Hash
$installedCatalogueHash = (Get-FileHash -LiteralPath $destinationCatalogue -Algorithm SHA256).Hash
if ($catalogueHash -ne $installedCatalogueHash) {
    throw "Installed Queen's Blood card catalogue does not match the repository source."
}
$enumeratorHash = (Get-FileHash -LiteralPath $sourceEnumerator -Algorithm SHA256).Hash
$installedEnumeratorHash = (Get-FileHash -LiteralPath $destinationEnumerator -Algorithm SHA256).Hash
if ($enumeratorHash -ne $installedEnumeratorHash) {
    throw "Installed card enumerator does not match the repository source."
}
$objectRefsHash = (Get-FileHash -LiteralPath $sourceObjectRefs -Algorithm SHA256).Hash
$installedObjectRefsHash = (Get-FileHash -LiteralPath $destinationObjectRefs -Algorithm SHA256).Hash
if ($objectRefsHash -ne $installedObjectRefsHash) {
    throw "Installed fresh object lookup module does not match the repository source."
}

$modsText = Get-Content -LiteralPath $modsFile -Raw
$entryPattern = '(?m)^' + [regex]::Escape($probeName) + '\s*:\s*\d+\s*$'
if ($modsText -match $entryPattern) {
    $modsText = [regex]::Replace($modsText, $entryPattern, "$probeName : 1")
}
else {
    $modsText = $modsText.TrimEnd() + "`r`n$probeName : 1`r`n"
}
[System.IO.File]::WriteAllText($modsFile, $modsText, (New-Object System.Text.UTF8Encoding($false)))

[pscustomobject]@{
    installed = $true
    probe = $probeName
    script = $destinationScript
    script_sha256 = $installedHash
    catalogue = $destinationCatalogue
    catalogue_sha256 = $installedCatalogueHash
    enumerator = $destinationEnumerator
    enumerator_sha256 = $installedEnumeratorHash
    object_refs = $destinationObjectRefs
    object_refs_sha256 = $installedObjectRefsHash
    backup = $backupRoot
}
