param(
  [ValidateSet("doctor", "start", "run", "stop", "logs", "status", "build", "help")]
  [string]$Action = "help",
  [ValidateSet("mock", "balanced", "accuracy", "selected", "cloud-stt", "cloud-elevenlabs", "cloud-soniox", "cloud-deepgram")]
  [string]$Mode = "balanced"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Ensure-Env {
  if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "[created] $Root\.env"
  }
}

function Import-DotEnv([string]$Path) {
  if (-not (Test-Path $Path)) { throw "$Path 파일이 없습니다." }
  foreach ($line in Get-Content $Path) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
      [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
  }
}

function Require-Key([string]$Name) {
  Import-DotEnv ".env"
  $value = [Environment]::GetEnvironmentVariable($Name, "Process")
  if ([string]::IsNullOrWhiteSpace($value)) { throw ".env에 $Name 값을 입력하세요." }
}

function Invoke-Compose([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args) {
  & docker compose @Args
  if ($LASTEXITCODE -ne 0) { throw "docker compose failed: $LASTEXITCODE" }
}

function Invoke-Up([string[]]$Profiles, [string[]]$Services, [switch]$Foreground) {
  $composeArgs = @()
  foreach ($profile in $Profiles) { $composeArgs += @("--profile", $profile) }
  $composeArgs += @("up", "--build")
  if (-not $Foreground) { $composeArgs += "-d" }
  $composeArgs += $Services
  Invoke-Compose @composeArgs
}

function Start-Local([string]$Model, [string]$MemoryUtil, [switch]$Foreground) {
  $env:STT_MODE="qwen_ws"; $env:TTS_MODE="qwen_ws"; $env:LLM_MODE="openai_compatible"
  $env:ASR_MODEL_ID=$Model
  $env:ASR_GPU_MEMORY_UTILIZATION=$MemoryUtil
  Invoke-Up -Profiles @("local-asr", "local-tts") -Services @("gateway", "qwen-asr", "qwen-tts") -Foreground:$Foreground
}

function Stop-LocalAsrQuietly {
  try { & docker compose --profile local-asr stop qwen-asr *> $null } catch {}
}

function Start-Cloud([string]$SttMode, [string]$Model, [string]$KeyName, [switch]$Foreground) {
  Require-Key $KeyName
  Stop-LocalAsrQuietly
  $env:STT_MODE=$SttMode; $env:STT_CLOUD_MODEL=$Model; $env:TTS_MODE="qwen_ws"; $env:LLM_MODE="openai_compatible"
  Invoke-Up -Profiles @("local-tts") -Services @("gateway", "qwen-tts") -Foreground:$Foreground
}

function Start-Selected([switch]$Foreground) {
  Ensure-Env
  Import-DotEnv ".env"
  Import-DotEnv "benchmark/selected_stt.env"
  if (-not $env:TTS_MODE) { $env:TTS_MODE="qwen_ws" }
  if (-not $env:LLM_MODE) { $env:LLM_MODE="openai_compatible" }
  switch ($env:STT_MODE) {
    "qwen_ws" {
      if (-not $env:ASR_MODEL_ID) { $env:ASR_MODEL_ID="Qwen/Qwen3-ASR-1.7B" }
      Invoke-Up -Profiles @("local-asr", "local-tts") -Services @("gateway", "qwen-asr", "qwen-tts") -Foreground:$Foreground
    }
    "elevenlabs_ws" { Require-Key "ELEVENLABS_API_KEY"; Stop-LocalAsrQuietly; Invoke-Up -Profiles @("local-tts") -Services @("gateway", "qwen-tts") -Foreground:$Foreground }
    "soniox_ws" { Require-Key "SONIOX_API_KEY"; Stop-LocalAsrQuietly; Invoke-Up -Profiles @("local-tts") -Services @("gateway", "qwen-tts") -Foreground:$Foreground }
    "deepgram_ws" { Require-Key "DEEPGRAM_API_KEY"; Stop-LocalAsrQuietly; Invoke-Up -Profiles @("local-tts") -Services @("gateway", "qwen-tts") -Foreground:$Foreground }
    default { throw "selected_stt.env의 STT_MODE을 지원하지 않습니다: $env:STT_MODE" }
  }
}

function Start-RequestedMode([string]$RequestedMode, [switch]$Foreground) {
  switch ($RequestedMode) {
    "mock" {
      try { & docker compose --profile local-asr --profile local-tts stop qwen-asr qwen-tts *> $null } catch {}
      $env:STT_MODE="mock"; $env:TTS_MODE="mock"; $env:LLM_MODE="mock"
      Invoke-Up -Profiles @() -Services @("gateway") -Foreground:$Foreground
    }
    "balanced" { Start-Local "Qwen/Qwen3-ASR-0.6B" "0.35" -Foreground:$Foreground }
    "accuracy" { Start-Local "Qwen/Qwen3-ASR-1.7B" "0.44" -Foreground:$Foreground }
    "selected" { Start-Selected -Foreground:$Foreground }
    "cloud-stt" { Start-Cloud "elevenlabs_ws" "scribe_v2_realtime" "ELEVENLABS_API_KEY" -Foreground:$Foreground }
    "cloud-elevenlabs" { Start-Cloud "elevenlabs_ws" "scribe_v2_realtime" "ELEVENLABS_API_KEY" -Foreground:$Foreground }
    "cloud-soniox" { Start-Cloud "soniox_ws" "stt-rt-v5" "SONIOX_API_KEY" -Foreground:$Foreground }
    "cloud-deepgram" { Start-Cloud "deepgram_ws" "nova-3" "DEEPGRAM_API_KEY" -Foreground:$Foreground }
    default { throw "지원하지 않는 실행 모드입니다: $RequestedMode" }
  }
}

function Write-UiUrls {
  $port = if ($env:GATEWAY_PORT) { $env:GATEWAY_PORT } else { "8080" }
  Write-Host "UI: http://localhost:$port"
  Write-Host "Health: http://localhost:$port/api/health"
}

function Stop-AllServices {
  try {
    Invoke-Compose --profile local-asr --profile local-tts down --remove-orphans
  } catch {
    Write-Warning "종료 정리 중 오류: $($_.Exception.Message)"
  }
}

if ($Action -eq "doctor") {
  Write-Host "== Persona Duplex doctor =="
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker가 없습니다." }
  & docker compose version
  & docker info | Out-Null
  if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    & nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
  } else { Write-Warning "nvidia-smi를 찾지 못했습니다." }
  try { Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 3 | Out-Null; Write-Host "Ollama: OK" }
  catch { Write-Warning "Ollama가 11434 포트에서 응답하지 않습니다." }
  exit 0
}

if ($Action -eq "start") {
  Ensure-Env
  Start-RequestedMode $Mode
  Write-UiUrls
  exit 0
}

if ($Action -eq "run") {
  Ensure-Env
  Write-Host "Persona Duplex를 포그라운드로 실행합니다. 종료하려면 Ctrl+C를 누르세요."
  Write-UiUrls
  try {
    Start-RequestedMode $Mode -Foreground
  } finally {
    Write-Host "실행된 Persona Duplex 서비스를 종료합니다..."
    Stop-AllServices
  }
  exit 0
}

switch ($Action) {
  "stop" { Invoke-Compose --profile local-asr --profile local-tts down --remove-orphans }
  "logs" { Invoke-Compose --profile local-asr --profile local-tts logs -f --tail=200 }
  "status" { Invoke-Compose --profile local-asr --profile local-tts ps }
  "build" { Invoke-Compose --profile local-asr --profile local-tts build }
  default {
    Write-Host @"
Usage:
  .\persona-duplex.ps1 doctor
  .\persona-duplex.ps1 start mock
  .\persona-duplex.ps1 start balanced
  .\persona-duplex.ps1 start accuracy
  .\persona-duplex.ps1 start selected
  .\persona-duplex.ps1 start cloud-elevenlabs
  .\persona-duplex.ps1 start cloud-soniox
  .\persona-duplex.ps1 start cloud-deepgram
  .\persona-duplex.ps1 run balanced
  .\persona-duplex.ps1 status|logs|stop|build
"@
  }
}
