param(
    [Parameter(Mandatory = $true)]
    [string]$TargetId,

    [Parameter(Mandatory = $true)]
    [double]$TargetX,

    [Parameter(Mandatory = $true)]
    [double]$TargetY,

    [Parameter(Mandatory = $true)]
    [double]$TargetZ,

    [double]$StartX,
    [double]$StartY,
    [double]$StartZ,
    [string]$OutputPath = 'C:\CaitGPT\Telemetry\navigation-native-target.json'
)

$hasStart = $PSBoundParameters.ContainsKey('StartX') -and
    $PSBoundParameters.ContainsKey('StartY') -and
    $PSBoundParameters.ContainsKey('StartZ')

if (($PSBoundParameters.ContainsKey('StartX') -or $PSBoundParameters.ContainsKey('StartY') -or $PSBoundParameters.ContainsKey('StartZ')) -and -not $hasStart) {
    throw 'StartX, StartY, and StartZ must be supplied together.'
}

$payload = [ordered]@{
    target_id = $TargetId
    position = [ordered]@{ x = $TargetX; y = $TargetY; z = $TargetZ }
}

if ($hasStart) {
    $payload.start_position = [ordered]@{ x = $StartX; y = $StartY; z = $StartZ }
}

$json = $payload | ConvertTo-Json -Depth 4
Set-Content -LiteralPath $OutputPath -Value $json -Encoding UTF8
Write-Output $json
