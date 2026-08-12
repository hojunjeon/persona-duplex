param(
  [ValidateSet("doctor", "start", "run", "stop", "logs", "status", "build", "help")]
  [string]$Action = "help",
  [ValidateSet("balanced", "accuracy", "selected", "cloud-stt", "cloud-elevenlabs", "cloud-soniox", "cloud-deepgram")]
  [string]$Mode = "balanced"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
$script:TunnelProcess = $null
$script:PublicTunnelUrl = $null
$script:TunnelKind = $null

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

function Test-DockerReady {
  try { & docker info *> $null } catch {}
  return ($LASTEXITCODE -eq 0)
}

function Ensure-Docker {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker CLI가 없습니다." }

  if (Test-DockerReady) { return }

  $desktopCandidates = @(
    (Join-Path ${env:ProgramFiles} "Docker\Docker\Docker Desktop.exe"),
    (Join-Path ${env:LOCALAPPDATA} "Programs\Docker\Docker Desktop.exe"),
    (Join-Path ${env:LOCALAPPDATA} "Programs\DockerDesktop\Docker Desktop.exe")
  ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
  if ($desktopCandidates.Count -eq 0) {
    throw "Docker Desktop을 찾지 못했습니다. Docker Desktop을 설치하고 다시 실행하세요."
  }

  $desktopRunning = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -eq "Docker Desktop" }
  if (-not $desktopRunning) {
    Write-Host "Docker Desktop을 시작합니다..."
    Start-Process -FilePath $desktopCandidates[0] -WorkingDirectory (Split-Path -Parent $desktopCandidates[0]) | Out-Null
  }

  $deadline = (Get-Date).AddSeconds(180)
  do {
    Start-Sleep -Seconds 3
    if (Test-DockerReady) {
      Write-Host "Docker Desktop 준비 완료"
      return
    }
  } while ((Get-Date) -lt $deadline)
  throw "Docker Desktop 데몬 준비 시간 초과입니다. Docker Desktop 상태를 확인하세요."
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

function Verify-RealStack([string]$RequestedMode) {
  Import-DotEnv ".env"
  $selectedStt = ""
  if ($RequestedMode -eq "selected" -and (Test-Path "benchmark/selected_stt.env")) {
    Import-DotEnv "benchmark/selected_stt.env"
    $selectedStt = [string]$env:STT_MODE
  }
  if ($RequestedMode -in @("balanced", "accuracy") -or $selectedStt -eq "qwen_ws") {
    $asrPort = if ($env:ASR_PORT) { $env:ASR_PORT } else { "8101" }
    Wait-Http "http://127.0.0.1:$asrPort/health" 240
    Invoke-Warmup "http://127.0.0.1:$asrPort/warmup" 900
  }
  if ($RequestedMode -in @("balanced", "accuracy", "selected", "cloud-stt", "cloud-elevenlabs", "cloud-soniox", "cloud-deepgram")) {
    $ttsPort = if ($env:TTS_PORT) { $env:TTS_PORT } else { "8102" }
    Wait-Http "http://127.0.0.1:$ttsPort/health" 240
    Invoke-Warmup "http://127.0.0.1:$ttsPort/warmup" 900
  }
  Ensure-LlmReady
  $gatewayPort = if ($env:GATEWAY_PORT) { $env:GATEWAY_PORT } else { "8080" }
  Wait-Http "http://127.0.0.1:$gatewayPort/api/health" 180
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:$gatewayPort/api/health" -TimeoutSec 15
  if (-not $health.ok) { throw "실전 스택 health가 통과하지 못했습니다: $($health | ConvertTo-Json -Depth 6 -Compress)" }
  Write-Host "Real stack: Qwen/LLM health OK"
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
    $ollamaComposeArgs = @("--profile", "local-llm", "up", "-d", "ollama")
    Invoke-Compose @ollamaComposeArgs
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

function Get-CloudflaredPath {
  if (-not [string]::IsNullOrWhiteSpace($env:CLOUDFLARED_PATH) -and (Test-Path -LiteralPath $env:CLOUDFLARED_PATH)) {
    return (Resolve-Path -LiteralPath $env:CLOUDFLARED_PATH).Path
  }
  $candidates = @(@(
    (Join-Path ${env:ProgramFiles} "cloudflared\cloudflared.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "cloudflared\cloudflared.exe"),
    (Join-Path ${env:LOCALAPPDATA} "cloudflared\cloudflared.exe")
  ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
  if ($candidates.Count -gt 0) { return @($candidates)[0] }
  $command = Get-Command cloudflared -ErrorAction SilentlyContinue
  if ($command -and $command.Path) { return $command.Path }
  if ($command -and $command.Source) { return $command.Source }
  return $null
}

function Get-TailscalePath {
  if (-not [string]::IsNullOrWhiteSpace($env:TAILSCALE_PATH) -and (Test-Path -LiteralPath $env:TAILSCALE_PATH)) {
    return (Resolve-Path -LiteralPath $env:TAILSCALE_PATH).Path
  }
  $candidates = @(
    (Join-Path ${env:ProgramFiles} "Tailscale\tailscale.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Tailscale\tailscale.exe"),
    (Join-Path ${env:LOCALAPPDATA} "Tailscale\tailscale.exe")
  ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
  if ($candidates.Count -gt 0) { return @($candidates)[0] }
  $command = Get-Command tailscale.exe -ErrorAction SilentlyContinue
  if ($command -and $command.Path) { return $command.Path }
  if ($command -and $command.Source) { return $command.Source }
  return $null
}

function Get-TailscaleFunnelUrl([string]$TailscalePath) {
  $jsonText = (& $TailscalePath funnel status --json 2>$null | Out-String).Trim()
  if ([string]::IsNullOrWhiteSpace($jsonText)) { return $null }
  try {
    $status = $jsonText | ConvertFrom-Json
    $entry = $status.Web.PSObject.Properties | Select-Object -First 1
    if ($entry -and $entry.Name) {
      return "https://$($entry.Name -replace ':443$','')"
    }
  } catch {}
  return $null
}

function Get-NpxPath {
  $command = Get-Command npx.cmd -ErrorAction SilentlyContinue
  if ($command -and $command.Path) { return $command.Path }
  $candidates = @(@(
    (Join-Path ${env:ProgramFiles} "nodejs\npx.cmd"),
    (Join-Path ${env:ProgramFiles(x86)} "nodejs\npx.cmd"),
    (Join-Path ${env:LOCALAPPDATA} "Programs\nodejs\npx.cmd")
  ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
  if ($candidates.Count -gt 0) { return $candidates[0] }
  return $null
}

function Get-NodePath {
  $command = Get-Command node.exe -ErrorAction SilentlyContinue
  if ($command -and $command.Path) { return $command.Path }
  $candidates = @(@(
    (Join-Path ${env:ProgramFiles} "nodejs\node.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "nodejs\node.exe"),
    (Join-Path ${env:LOCALAPPDATA} "Programs\nodejs\node.exe")
  ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
  if ($candidates.Count -gt 0) { return $candidates[0] }
  return $null
}

function Get-LocalTunnelScript {
  $candidates = @(@(
    (Join-Path ${env:APPDATA} "npm\node_modules\localtunnel\bin\lt.js"),
    (Join-Path ${env:ProgramFiles} "nodejs\node_modules\localtunnel\bin\lt.js")
  ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
  if ($candidates.Count -gt 0) { return $candidates[0] }
  return $null
}

function Start-PublicTunnel {
  Import-DotEnv ".env"
  $tunnelMode = if ([string]::IsNullOrWhiteSpace($env:PUBLIC_TUNNEL)) { "tailscale" } else { $env:PUBLIC_TUNNEL.ToLowerInvariant() }
  if ($tunnelMode -in @("off", "none", "false", "0")) {
    Write-Host "외부 터널: 비활성화 (PUBLIC_TUNNEL=$tunnelMode)"
    return
  }
  $script:TunnelKind = $tunnelMode
  $port = if ($env:GATEWAY_PORT) { $env:GATEWAY_PORT } else { "8080" }

  if ($tunnelMode -eq "tailscale") {
    $tailscale = Get-TailscalePath
    if (-not $tailscale) {
      throw "Tailscale을 찾지 못했습니다. Tailscale을 설치하고 로그인한 뒤 다시 실행하세요."
    }
    Write-Host "Tailscale Funnel을 시작합니다 (127.0.0.1:$port)..."
    & $tailscale funnel --bg $port 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
      throw "Tailscale Funnel 시작 실패입니다. `tailscale funnel $port`를 직접 확인하세요."
    }
    $script:TunnelKind = "tailscale"
    $deadline = (Get-Date).AddSeconds(30)
    do {
      Start-Sleep -Seconds 1
      $script:PublicTunnelUrl = Get-TailscaleFunnelUrl $tailscale
    } while ([string]::IsNullOrWhiteSpace($script:PublicTunnelUrl) -and (Get-Date) -lt $deadline)
    if ([string]::IsNullOrWhiteSpace($script:PublicTunnelUrl)) {
      Stop-PublicTunnel
      throw "Tailscale Funnel URL을 확인하지 못했습니다. `tailscale funnel status`를 확인하세요."
    }

    $healthReady = $false
    $healthDeadline = (Get-Date).AddSeconds(45)
    do {
      try {
        Invoke-RestMethod -Uri "$($script:PublicTunnelUrl)/api/health" -TimeoutSec 8 | Out-Null
        $healthReady = $true
        break
      } catch { Start-Sleep -Seconds 2 }
    } while ((Get-Date) -lt $healthDeadline)
    if (-not $healthReady) {
      $failedUrl = $script:PublicTunnelUrl
      Stop-PublicTunnel
      throw "Tailscale Funnel URL은 생성됐지만 외부 Health 요청이 시간 초과했습니다: $failedUrl"
    }
    Write-Host "Public UI: $script:PublicTunnelUrl"
    Write-Host "Public Health: $script:PublicTunnelUrl/api/health"
    return
  }

  $executable = $null
  $arguments = $null
  $urlPattern = $null
  $workingDirectory = $Root
  $timeoutSec = 120
  switch ($tunnelMode) {
    "localtunnel" {
      $localTunnelScript = Get-LocalTunnelScript
      if ($localTunnelScript) {
        $executable = Get-NodePath
        if (-not $executable) { throw "Node.js가 없습니다. Node.js를 설치하세요." }
        $arguments = @($localTunnelScript, "--port", $port, "--local-host", "127.0.0.1")
        $workingDirectory = Split-Path -Parent $executable
      } else {
        $executable = Get-NpxPath
        if (-not $executable) { throw "npx가 없습니다. Node.js를 설치하세요." }
        $arguments = @("--yes", "localtunnel", "--port", $port, "--local-host", "127.0.0.1")
        $workingDirectory = Split-Path -Parent $executable
      }
      $urlPattern = "https://[a-z0-9-]+\.loca\.lt"
      break
    }
    "quick" {
      $executable = Get-CloudflaredPath
      if (-not $executable) {
        throw "cloudflared가 없습니다. `winget install --id Cloudflare.cloudflared --exact`를 먼저 실행하세요."
      }
      $arguments = @("tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:$port")
      $urlPattern = "https://[a-z0-9-]+\.trycloudflare\.com"
      $workingDirectory = Split-Path -Parent $executable
      $timeoutSec = 45
      break
    }
    default { throw "지원하지 않는 PUBLIC_TUNNEL 값입니다: $tunnelMode (tailscale, localtunnel, quick 또는 off 사용)" }
  }
  $logDir = Join-Path $env:TEMP "persona-duplex-public-tunnel"
  New-Item -ItemType Directory -Force -Path $logDir | Out-Null
  $stdoutPath = Join-Path $logDir "stdout.log"
  $stderrPath = Join-Path $logDir "stderr.log"
  Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue

  Write-Host "외부 터널을 시작합니다..."
  if ([string]::IsNullOrWhiteSpace($workingDirectory)) { $workingDirectory = $Root }
  $script:TunnelProcess = Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory $workingDirectory -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -WindowStyle Hidden -PassThru
  $deadline = (Get-Date).AddSeconds($timeoutSec)
  do {
    Start-Sleep -Seconds 1
    $output = ""
    foreach ($path in @($stdoutPath, $stderrPath)) {
      if (Test-Path -LiteralPath $path) {
        try { $output += "`n" + (Get-Content -Raw -LiteralPath $path -ErrorAction SilentlyContinue) } catch {}
      }
    }
    if ($output -match $urlPattern) {
      $script:PublicTunnelUrl = $Matches[0].TrimEnd(".")
      break
    }
    if ($script:TunnelProcess.HasExited) { break }
  } while ((Get-Date) -lt $deadline)

  if ([string]::IsNullOrWhiteSpace($script:PublicTunnelUrl)) {
    $errorText = if (Test-Path -LiteralPath $stderrPath) { Get-Content -Raw -LiteralPath $stderrPath } else { "출력 없음" }
    Stop-PublicTunnel
    throw "외부 터널 시작 실패: $errorText"
  }

  $healthReady = $false
  $healthDeadline = (Get-Date).AddSeconds(45)
  do {
    try {
      Invoke-RestMethod -Uri "$($script:PublicTunnelUrl)/api/health" -TimeoutSec 8 | Out-Null
      $healthReady = $true
      break
    } catch { Start-Sleep -Seconds 2 }
  } while ((Get-Date) -lt $healthDeadline)
  if (-not $healthReady) {
    $failedUrl = $script:PublicTunnelUrl
    Stop-PublicTunnel
    throw "외부 터널 URL은 생성됐지만 외부 Health 요청이 시간 초과했습니다: $failedUrl"
  }
  Write-Host "Public UI: $script:PublicTunnelUrl"
  Write-Host "Public Health: $script:PublicTunnelUrl/api/health"
}

function Stop-PublicTunnel {
  $configuredTunnelMode = "tailscale"
  try {
    Import-DotEnv ".env"
    if (-not [string]::IsNullOrWhiteSpace($env:PUBLIC_TUNNEL)) {
      $configuredTunnelMode = $env:PUBLIC_TUNNEL.ToLowerInvariant()
    }
  } catch {}
  if ($script:TunnelProcess) {
    try {
      if (-not $script:TunnelProcess.HasExited) { & taskkill.exe /PID $script:TunnelProcess.Id /T /F *> $null }
    } catch {}
  }
  $shouldResetTailscale = $script:TunnelKind -eq "tailscale" -or ([string]::IsNullOrWhiteSpace($script:TunnelKind) -and $configuredTunnelMode -eq "tailscale")
  if ($shouldResetTailscale) {
    $tailscale = Get-TailscalePath
    if ($tailscale) {
      try { & $tailscale funnel reset *> $null } catch {}
    }
  }
  $script:TunnelProcess = $null
  $script:PublicTunnelUrl = $null
  $script:TunnelKind = $null
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
  # The gateway serves source files from the image. Always rebuild local
  # source-backed services so a launcher restart cannot keep an older image
  # and hide newly implemented UI/API features.
  $buildRequired = $false
  foreach ($service in $Services) {
    $image = switch ($service) {
      "gateway" { "persona-duplex-gateway"; break }
      "qwen-asr" { "persona-duplex-qwen-asr"; break }
      "qwen-tts" { "persona-duplex-qwen-tts"; break }
      default { $null }
    }
    if ($image) {
      $buildRequired = $true
      & docker image inspect $image *> $null
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
  Ensure-Ollama
  switch ($RequestedMode) {
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
  Ensure-Docker
  & docker compose version
  if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    & nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
  } else { Write-Warning "nvidia-smi를 찾지 못했습니다." }
  try { Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 3 | Out-Null; Write-Host "Ollama: OK" }
  catch { Write-Warning "Ollama가 11434 포트에서 응답하지 않습니다." }
  exit 0
}

if ($Action -eq "start") {
  Ensure-Env
  Ensure-Docker
  try {
    Start-RequestedMode $Mode
    $gatewayPort = if ($env:GATEWAY_PORT) { $env:GATEWAY_PORT } else { "8080" }
    Wait-Http "http://127.0.0.1:$gatewayPort/api/config" 180
    Verify-RealStack $Mode
    Start-PublicTunnel
    Write-UiUrls
  } catch {
    Stop-PublicTunnel
    Stop-AllServices
    throw
  }
  exit 0
}

if ($Action -eq "run") {
  Ensure-Env
  Ensure-Docker
  Write-Host "Persona Duplex를 포그라운드로 실행합니다. 종료하려면 Ctrl+C를 누르세요."
  try {
    Start-RequestedMode $Mode
    $gatewayPort = if ($env:GATEWAY_PORT) { $env:GATEWAY_PORT } else { "8080" }
    Wait-Http "http://127.0.0.1:$gatewayPort/api/config" 180
    Verify-RealStack $Mode
    Start-PublicTunnel
    Write-UiUrls
    Invoke-Compose --profile local-asr --profile local-tts --profile local-llm logs -f --tail=200
  } finally {
    Stop-PublicTunnel
    Write-Host "실행된 Persona Duplex 서비스를 종료합니다..."
    Stop-AllServices
  }
  exit 0
}

switch ($Action) {
  "stop" { Stop-PublicTunnel; Invoke-Compose --profile local-asr --profile local-tts --profile local-llm down --remove-orphans }
  "logs" { Invoke-Compose --profile local-asr --profile local-tts --profile local-llm logs -f --tail=200 }
  "status" { Invoke-Compose --profile local-asr --profile local-tts --profile local-llm ps }
  "build" { Invoke-Compose --profile local-asr --profile local-tts --profile local-llm build }
  default {
    Write-Host @"
Usage:
  .\persona-duplex.ps1 doctor
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
