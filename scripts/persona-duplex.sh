#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
ACTION="${1:-help}"
MODE="${2:-balanced}"

compose() { docker compose "$@"; }

ensure_env() {
  if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "[created] $ROOT/.env"
  fi
}

load_env_file() {
  local file="$1"
  [[ -f "$file" ]] || { echo "ERROR: $file 파일이 없습니다."; exit 1; }
  set -a
  # shellcheck disable=SC1090
  source "$file"
  set +a
}

require_key() {
  local name="$1"
  load_env_file .env
  [[ -n "${!name:-}" ]] || { echo "ERROR: .env에 $name 값을 입력하세요."; exit 1; }
}

start_local() {
  local model="$1" util="$2"
  export STT_MODE=qwen_ws TTS_MODE=qwen_ws LLM_MODE="${LLM_MODE:-openai_compatible}"
  export ASR_MODEL_ID="$model" ASR_GPU_MEMORY_UTILIZATION="$util"
  compose --profile local-asr --profile local-tts up --build -d gateway qwen-asr qwen-tts
}

start_cloud() {
  local mode="$1" model="$2" key="$3"
  require_key "$key"
  # A previously running local ASR keeps its vLLM reservation unless stopped.
  compose --profile local-asr stop qwen-asr >/dev/null 2>&1 || true
  export STT_MODE="$mode" STT_CLOUD_MODEL="$model" TTS_MODE=qwen_ws LLM_MODE="${LLM_MODE:-openai_compatible}"
  compose --profile local-tts up --build -d gateway qwen-tts
}

start_selected() {
  ensure_env
  load_env_file .env
  load_env_file benchmark/selected_stt.env
  export TTS_MODE="${TTS_MODE:-qwen_ws}" LLM_MODE="${LLM_MODE:-openai_compatible}"
  case "${STT_MODE:-}" in
    qwen_ws)
      export ASR_MODEL_ID="${ASR_MODEL_ID:-Qwen/Qwen3-ASR-1.7B}"
      compose --profile local-asr --profile local-tts up --build -d gateway qwen-asr qwen-tts
      ;;
    elevenlabs_ws) require_key ELEVENLABS_API_KEY; compose --profile local-asr stop qwen-asr >/dev/null 2>&1 || true; compose --profile local-tts up --build -d gateway qwen-tts ;;
    soniox_ws) require_key SONIOX_API_KEY; compose --profile local-asr stop qwen-asr >/dev/null 2>&1 || true; compose --profile local-tts up --build -d gateway qwen-tts ;;
    deepgram_ws) require_key DEEPGRAM_API_KEY; compose --profile local-asr stop qwen-asr >/dev/null 2>&1 || true; compose --profile local-tts up --build -d gateway qwen-tts ;;
    *) echo "ERROR: selected_stt.env의 STT_MODE을 지원하지 않습니다: ${STT_MODE:-<empty>}"; exit 2 ;;
  esac
}

doctor() {
  echo "== Persona Duplex doctor =="
  command -v docker >/dev/null || { echo "ERROR: Docker가 없습니다."; exit 1; }
  docker compose version
  if command -v nvidia-smi >/dev/null; then
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
  else
    echo "WARN: nvidia-smi를 찾지 못했습니다."
  fi
  docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon에 연결할 수 없습니다."; exit 1; }
  echo "Docker daemon: OK"
  if command -v curl >/dev/null && curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "Ollama: OK"
  else
    echo "WARN: Ollama가 11434 포트에서 응답하지 않습니다."
  fi
  echo "Browser microphone: localhost에서는 허용됩니다. 원격 접속은 HTTPS가 필요합니다."
}

start_mode() {
  ensure_env
  case "$1" in
    mock)
      compose --profile local-asr --profile local-tts stop qwen-asr qwen-tts >/dev/null 2>&1 || true
      export STT_MODE=mock TTS_MODE=mock LLM_MODE=mock
      compose up --build -d gateway
      ;;
    balanced) start_local Qwen/Qwen3-ASR-0.6B 0.35 ;;
    accuracy) start_local Qwen/Qwen3-ASR-1.7B 0.44 ;;
    selected) start_selected ;;
    cloud-stt|cloud-elevenlabs) start_cloud elevenlabs_ws scribe_v2_realtime ELEVENLABS_API_KEY ;;
    cloud-soniox) start_cloud soniox_ws stt-rt-v5 SONIOX_API_KEY ;;
    cloud-deepgram) start_cloud deepgram_ws nova-3 DEEPGRAM_API_KEY ;;
    *) echo "Unknown mode: $1"; exit 2 ;;
  esac
  echo "UI: http://localhost:${GATEWAY_PORT:-8080}"
  echo "Health: http://localhost:${GATEWAY_PORT:-8080}/api/health"
}

case "$ACTION" in
  doctor) doctor ;;
  start) start_mode "$MODE" ;;
  stop) compose --profile local-asr --profile local-tts down --remove-orphans ;;
  logs) compose --profile local-asr --profile local-tts logs -f --tail=200 "${3:-}" ;;
  status) compose --profile local-asr --profile local-tts ps ;;
  build) compose --profile local-asr --profile local-tts build ;;
  *)
    cat <<'USAGE'
Usage:
  ./persona-duplex.sh doctor
  ./persona-duplex.sh start mock
  ./persona-duplex.sh start balanced
  ./persona-duplex.sh start accuracy
  ./persona-duplex.sh start selected
  ./persona-duplex.sh start cloud-elevenlabs
  ./persona-duplex.sh start cloud-soniox
  ./persona-duplex.sh start cloud-deepgram
  ./persona-duplex.sh status|logs|stop|build
USAGE
    ;;
esac
