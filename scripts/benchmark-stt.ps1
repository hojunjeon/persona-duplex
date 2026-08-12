param(
  [ValidateSet("run", "select", "apply", "all", "help")]
  [string]$Action = "help",
  [string]$Value = "qwen,elevenlabs,soniox,deepgram"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
$Results = "data/benchmark/results.csv"

function Run-Benchmark([string]$Providers) {
  if (-not (Test-Path "data/benchmark/manifest.csv")) { throw "http://localhost:8080/benchmark 에서 시험 문장을 먼저 녹음하세요." }
  & docker compose --profile benchmark run --rm stt-benchmark python benchmark/benchmark_stt.py `
    --manifest /data/benchmark/manifest.csv --providers $Providers --output /data/benchmark/results.csv
  if ($LASTEXITCODE -ne 0) { throw "STT benchmark failed: $LASTEXITCODE" }
}
function Select-Best([string]$Policy) {
  if ($Policy -notin @("accuracy", "balanced", "latency")) { $Policy = "balanced" }
  & python benchmark/select_best.py $Results --policy $Policy --output benchmark/selected_stt.env
  if ($LASTEXITCODE -ne 0) { throw "STT selection failed: $LASTEXITCODE" }
}
function Apply-Best {
  if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
  & python benchmark/apply_selection.py --selection benchmark/selected_stt.env --target .env
  if ($LASTEXITCODE -ne 0) { throw "STT selection apply failed: $LASTEXITCODE" }
}

switch ($Action) {
  "run" { Run-Benchmark $Value }
  "select" { Select-Best $Value }
  "apply" { Apply-Best }
  "all" { Run-Benchmark $Value; Select-Best "balanced"; Apply-Best }
  default {
    Write-Host @"
Usage:
  .\scripts\benchmark-stt.ps1 run qwen,elevenlabs,soniox,deepgram
  .\scripts\benchmark-stt.ps1 select accuracy
  .\scripts\benchmark-stt.ps1 apply
  .\scripts\benchmark-stt.ps1 all qwen,elevenlabs,soniox,deepgram
"@
  }
}
