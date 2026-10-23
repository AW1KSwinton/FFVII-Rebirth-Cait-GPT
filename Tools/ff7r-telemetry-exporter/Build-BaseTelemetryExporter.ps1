param(
    [string]$BuildDirectory = (Join-Path $PSScriptRoot 'Base\Build'),
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'

$gameProcessNames = @('ff7rebirth_', 'ff7rebirth')
if (Get-Process -ErrorAction SilentlyContinue | Where-Object { $gameProcessNames -contains $_.ProcessName }) {
    throw 'Refusing to build/deploy base telemetry exporter while Rebirth is running.'
}

$cmake = (Get-Command cmake -ErrorAction Stop).Source
$cmakeLists = Join-Path $PSScriptRoot 'Base\CMakeLists.txt'
$sourceRoot = $env:UE4SS_SOURCE
$pseudoRoot = $env:UEPSEUDO_SOURCE
$imguiRoot = $env:IMGUI_SOURCE
$imguiTextRoot = $env:IMGUI_TEXT_EDIT_SOURCE
$importLib = $env:UE4SS_IMPORT_LIB

foreach ($required in @('UE4SS_SOURCE', 'UEPSEUDO_SOURCE', 'IMGUI_SOURCE', 'IMGUI_TEXT_EDIT_SOURCE', 'UE4SS_IMPORT_LIB')) {
    $configured = [Environment]::GetEnvironmentVariable($required)
    if (-not $configured -or -not (Test-Path -LiteralPath $configured)) {
        throw "Set $required to the matching SDK/source path before building."
    }
}

New-Item -ItemType Directory -Force $BuildDirectory | Out-Null
$interfaceRoot = Join-Path $BuildDirectory 'installed-interface'
& $Python (Join-Path $PSScriptRoot 'Prepare-InstalledUe4ssInterface.py') $sourceRoot $interfaceRoot
if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the pinned installed-build interface.' }
& $cmake -S (Join-Path $PSScriptRoot 'Base') -B $BuildDirectory -G 'Visual Studio 17 2022' -A x64 `
    "-DINSTALLED_INTERFACE=$interfaceRoot" `
    "-DUE4SS_SOURCE=$sourceRoot" `
    "-DUEPSEUDO_SOURCE=$pseudoRoot" `
    "-DIMGUI_SOURCE=$imguiRoot" `
    "-DIMGUI_TEXT_EDIT_SOURCE=$imguiTextRoot" `
    "-DZYDIS_SOURCE=$sourceRoot/../../header-deps/zydis-v4.1.0" `
    "-DUE4SS_IMPORT_LIB=$importLib"
if ($LASTEXITCODE -ne 0) {
    throw "CMake configure failed with exit code $LASTEXITCODE"
}
& $cmake --build $BuildDirectory --config Release -- /m
if ($LASTEXITCODE -ne 0) {
    throw "CMake build failed with exit code $LASTEXITCODE"
}

& (Join-Path $BuildDirectory 'Release\FF7RTelemetrySampleTests.exe')
if ($LASTEXITCODE -ne 0) { throw 'Native snapshot parser regression failed.' }

& (Join-Path $BuildDirectory 'Release\FF7RObjectPathCacheTests.exe')
if ($LASTEXITCODE -ne 0) { throw 'Native object-path identity regression failed.' }

$dll = Join-Path $BuildDirectory 'Release\main.dll'
if (-not (Test-Path -LiteralPath $dll)) {
    throw "Telemetry build did not produce $dll"
}
Get-FileHash -Algorithm SHA256 -LiteralPath $dll
