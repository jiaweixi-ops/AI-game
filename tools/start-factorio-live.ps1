param(
    [string]$FactorioExe = $env:FACTORIO_EXE,
    [string]$Save = "",
    [int]$UdpPort = 34198,
    [int]$PlayerIndex = 0,
    [switch]$ProbeMove,
    [switch]$StartAI
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "[1/4] Installing GAR Python package..."
python -m pip install -e .

$launchArgs = @("--udp-port", "$UdpPort")
if ($FactorioExe) { $launchArgs += @("--factorio-exe", $FactorioExe) }
if ($Save) { $launchArgs += @("--save", $Save) }
if ($PlayerIndex -gt 0) { $launchArgs += @("--player-index", "$PlayerIndex") }

Write-Host "[2/4] Installing bridge mod and starting Factorio..."
& gar-factorio-launch @launchArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$probeArgs = @("--port", "$UdpPort")
if ($PlayerIndex -gt 0) { $probeArgs += @("--player-index", "$PlayerIndex") }
if ($ProbeMove) { $probeArgs += "--write-move-round-trip" }

Write-Host "[3/4] Running live contract probes..."
& gar-factorio-probe @probeArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not $StartAI) {
    Write-Host "[4/4] Live bridge verified. AI controller not started (use -StartAI when API env vars are configured)."
    exit 0
}

if (-not $env:GAR_AI_ENDPOINT) { throw "GAR_AI_ENDPOINT is required for -StartAI" }
if (-not $env:GAR_AI_MODEL) { throw "GAR_AI_MODEL is required for -StartAI" }
if (-not $env:GAR_AI_API_KEY) { throw "GAR_AI_API_KEY is required for -StartAI" }

$env:GAR_FACTORIO_UDP_PORT = "$UdpPort"
if ($PlayerIndex -gt 0) { $env:GAR_FACTORIO_PLAYER_INDEX = "$PlayerIndex" }

Write-Host "[4/4] Starting GAR AI controller against the live Factorio bridge..."
& gar-ai --bridge-factory gar_ai.factorio_udp_bridge:create_bridge --runtime-dir runtime/live
exit $LASTEXITCODE
