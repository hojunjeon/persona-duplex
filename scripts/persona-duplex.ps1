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

function Wait-Http([string]$Url, [int]$TimeoutSec = 180) {
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  do {
    try {
      Invoke-RestMethod -Uri $Url -TimeoutSec 5 | Out-Null
      return
    } catch {
      Start-Sleep -Seconds 2
    }
  } while ((Get-Date) -lt $deadline)
  throw "서비스 준비 시간 초과: $Url"
}

function Invoke-Warmup([string]$Url, [int]$TimeoutSec = 900) {
  try {
    Invoke-RestMethod -Method Post -Uri $Url -TimeoutSec $TimeoutSec | Out-Null
  } catch {
    throw "모델 warmup 실패 ($Url): $($_.Exception.Message)"
  }
}

function Ensure-Ollama {
  Import-DotEnv ".env"
  if ([string]::IsNullOrWhiteSpace($env:OLLAMA_PORT)) { $env:OLLAMA_PORT = "11434" }
  if ([string]::IsNullOrWhiteSpace($env:LLM_MODEL)) { $env:LLM_MODEL = "qwen3:1.7b" }
  if ([string]::IsNullOrWhiteSpace($env:LLM_BASE_URL)) {
    $env:LLM_BASE_URL = "http://host.docker.internal:$($env:OLLAMA_PORT)/v1"
  }

  $ollamaBase = "http://127.0.0.1:$($env:OLLAMA_PORT)"
  $hostReady = $false
  try {
    Invoke-RestMethod -Uri "$ollamaBase/api/tags" -TimeoutSec 5 | Out-Null
    $hostReady = $true
  } catch {}

  if (-not $hostReady) {
    Write-Host "Ollama 서버를 시작합니다..."
    Invoke-Compose --profile local-llm up -d ollama
    Wait-Http "$ollamaBase/api/tags" 180
  }

  $models = Invoke-RestMethod -Uri "$ollamaBase/api/tags" -TimeoutSec 10
  $found = @($models.models | Where-Object { $_.name -eq $env:LLM_MODEL -or $_.model -eq $env:LLM_MODEL })
  if ($found.Count -eq 0) {
    Write-Host "Ollama 모델을 다운로드합니다: $env:LLM_MODEL"
    if ($hostReady -and (Get-Command ollama -ErrorAction SilentlyContinue)) {
      & ollama pull $env:LLM_MODEL
    } else {
      & docker compose --profile local-llm exec -T ollama ollama pull $env:LLM_MODEL
    }
    if ($LASTEXITCODE -ne 0) { throw "Ollama 모델 다운로드 실패: $env:LLM_MODEL" }
  }

  $models = Invoke-RestMethod -Uri "$ollamaBase/api/tags" -TimeoutSec 10
  $found = @($models.models | Where-Object { $_.name -eq $env:LLM_MODEL -or $_.model -eq $env:LLM_MODEL })
  if ($found.Count -eq 0) { throw "Ollama 모델을 찾지 못했습니다: $env:LLM_MODEL" }
}

function Ensure-QwenReady([switch]$Asr, [switch]$Tts) {
  if ([string]::IsNullOrWhiteSpace($env:ASR_PORT)) { $env:ASR_PORT = "8101" }
  if ([string]::IsNullOrWhiteSpace($env:TTS_PORT)) { $env:TTS_PORT = "8102" }

  if ($Asr) {
    $base = "http://127.0.0.1:$($env:ASR_PORT)"
    Wait-Http "$base/health" 180
    Write-Host "Qwen ASR 모델을 warmup합니다..."
    Invoke-Warmup "$base/warmup" 900
    $health = Invoke-RestMethod -Uri "$base/health" -TimeoutSec 10
    if (-not $health.loaded) { throw "Qwen ASR 모델이 로드되지 않았습니다." }
    Write-Host "Qwen ASR 준비 완료: $($health.model) / $($health.device)"
  }

  if ($Tts) {
    $base = "http://127.0.0.1:$($env:TTS_PORT)"
    Wait-Http "$base/health" 180
    Write-Host "Qwen TTS 모델을 warmup합니다..."
    Invoke-Warmup "$base/warmup" 900
    $health = Invoke-RestMethod -Uri "$base/health" -TimeoutSec 10
    if (-not $health.loaded) { throw "Qwen TTS 모델이 로드되지 않았습니다." }
    Write-Host "Qwen TTS 준비 완료: $($health.model) / $($health.loaded_backend)"
  }
}

function Ensure-LlmReady {
  $port = if ($env:OLLAMA_PORT) { $env:OLLAMA_PORT } else { "11434" }
  $body = @{
    model = $env:LLM_MODEL
    messages = @(@{ role = "user"; content = "Reply with exactly OK." })
    stream = $false
    options = @{ num_predict = 8 }
  } | ConvertTo-Json -Depth 6
  try {
    $response = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/v1/chat/completions" -Body $body -ContentType "application/json" -TimeoutSec 180
    $content = [string]$response.choices[0].message.content
    if ([string]::IsNullOrWhiteSpace($content)) { throw "빈 응답" }
    Write-Host "LLM 준비 완료: $env:LLM_MODEL"
  } catch {
    throw "LLM 실전 요청 실패 ($env:LLM_MODEL): $($_.Exception.Message)"
  }
}

function Complete-RealStart([switch]$Asr, [switch]$Tts, [switch]$Foreground, [string[]]$Services) {
  Ensure-QwenReady -Asr:$Asr -Tts:$Tts
  Ensure-LlmReady
  if ($Foreground) {
    $logArgs = @("--profile", "local-asr", "--profile", "local-tts", "--profile", "local-llm", "logs", "-f", "--tail=200") + $Services
    Invoke-Compose @logArgs
  }
}

function Invoke-Up([string[]]$Profiles, [string[]]$Services, [switch]$Foreground) {
  $composeArgs = @()
  foreach ($profile in $Profiles) { $composeArgs += @("--profile", $profile) }
  $composeArgs += @("up")
  $buildRequired = $false
  foreach ($service in $Services) {
    $image = switch ($service) {
      "gateway" { "persona-duplex-gateway"; break }
      "qwen-asr" { "persona-duplex-qwen-asr"; break }
      "qwen-tts" { "persona-duplex-qwen-tts"; break }
      default { $null }
    }
    if ($image) {
      & docker image inspect $image *> $null
      if ($LASTEXITCODE -ne 0) { $buildRequired = $true }
    }
  }
  if ($buildRequired) { $composeArgs += "--build" }
  if (-not $Foreground) { $composeArgs += "-d" }
  $composeArgs += $Services
  Invoke-Compose @composeArgs
}

function Start-Local([string]$Model, [string]$MemoryUtil, [switch]$Foreground) {
  $env:STT_MODE="qwen_ws"; $env:TTS_MODE="qwen_ws"; $env:LLM_MODE="openai_compatible"
  $env:ASR_MODEL_ID=$Model
  $env:ASR_GPU_MEMORY_UTILIZATION=$MemoryUtil
  Invoke-Up -Profiles @("local-asr", "local-tts") -Services @("gateway", "qwen-asr", "qwen-tts")
  Complete-RealStart -Asr -Tts -Foreground:$Foreground -Services @("gateway", "qwen-asr", "qwen-tts")
}

function Stop-LocalAsrQuietly {
  try { & docker compose --profile local-asr stop qwen-asr *> $null } catch {}
}

function Start-Cloud([string]$SttMode, [string]$Model, [string]$KeyName, [switch]$Foreground) {
  Require-Key $KeyName
  Stop-LocalAsrQuietly
  $env:STT_MODE=$SttMode; $env:STT_CLOUD_MODEL=$Model; $env:TTS_MODE="qwen_ws"; $env:LLM_MODE="openai_compatible"
  Invoke-Up -Profiles @("local-tts") -Services @("gateway", "qwen-tts")
  Complete-RealStart -Tts -Foreground:$Foreground -Services @("gateway", "qwen-tts")
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
      Invoke-Up -Profiles @("local-asr", "local-tts") -Services @("gateway", "qwen-asr", "qwen-tts")
      Complete-RealStart -Asr -Tts -Foreground:$Foreground -Services @("gateway", "qwen-asr", "qwen-tts")
    }
    "elevenlabs_ws" { Require-Key "ELEVENLABS_API_KEY"; Stop-LocalAsrQuietly; Invoke-Up -Profiles @("local-tts") -Services @("gateway", "qwen-tts"); Complete-RealStart -Tts -Foreground:$Foreground -Services @("gateway", "qwen-tts") }
    "soniox_ws" { Require-Key "SONIOX_API_KEY"; Stop-LocalAsrQuietly; Invoke-Up -Profiles @("local-tts") -Services @("gateway", "qwen-tts"); Complete-RealStart -Tts -Foreground:$Foreground -Services @("gateway", "qwen-tts") }
    "deepgram_ws" { Require-Key "DEEPGRAM_API_KEY"; Stop-LocalAsrQuietly; Invoke-Up -Profiles @("local-tts") -Services @("gateway", "qwen-tts"); Complete-RealStart -Tts -Foreground:$Foreground -Services @("gateway", "qwen-tts") }
    default { throw "selected_stt.env의 STT_MODE을 지원하지 않습니다: $env:STT_MODE" }
  }
}

function Start-RequestedMode([string]$RequestedMode, [switch]$Foreground) {
  if ($RequestedMode -ne "mock") { Ensure-Ollama }
  switch ($RequestedMode) {
    "mock" {
      try { & docker compose --profile local-asr --profile local-tts stop qwen-asr qwen-tts *> $null } catch {}
      $env:STT_MODE="mock"; $env:TTS_MODE="mock"; $env:LLM_MODE="mock"
      Invoke-Up -Profiles @() -Services @("gateway") -Foreground:$Foreground
    }
    "balanced" { Start-Local "Qwen/Qwen3-ASR-0.6B" "0.75" -Foreground:$Foreground }
    "accuracy" { Start-Local "Qwen/Qwen3-ASR-1.7B" "0.80" -Foreground:$Foreground }
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
    Invoke-Compose --profile local-asr --profile local-tts --profile local-llm down --remove-orphans
  } catch {
    Write-Warning "종료 정리 중 오류: $($_.Exception.Message)"
  }
}

if ($Action -eq "doctor") {
  Write-Host "== Persona Duplex doctor =="
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker가 없습니다." }
  & docker compose version
  & docker info | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Docker Desktop 데몬에 연결할 수 없습니다." }
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
  try {
    Start-RequestedMode $Mode -Foreground
    Write-UiUrls
  } finally {
    Write-Host "실행된 Persona Duplex 서비스를 종료합니다..."
    Stop-AllServices
  }
  exit 0
}

switch ($Action) {
  "stop" { Invoke-Compose --profile local-asr --profile local-tts --profile local-llm down --remove-orphans }
  "logs" { Invoke-Compose --profile local-asr --profile local-tts --profile local-llm logs -f --tail=200 }
  "status" { Invoke-Compose --profile local-asr --profile local-tts --profile local-llm ps }
  "build" { Invoke-Compose --profile local-asr --profile local-tts --profile local-llm build }
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
