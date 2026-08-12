#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
ACTION="${1:-help}"
PROVIDERS="${2:-qwen,elevenlabs,soniox,deepgram}"
RESULTS="data/benchmark/results.csv"
MANIFEST="/data/benchmark/manifest.csv"

case "$ACTION" in
  run)
    [[ -f data/benchmark/manifest.csv ]] || { echo "ERROR: http://localhost:8080/benchmark 에서 시험 문장을 먼저 녹음하세요."; exit 1; }
    docker compose --profile benchmark run --rm stt-benchmark \
      python benchmark/benchmark_stt.py --manifest "$MANIFEST" --providers "$PROVIDERS" --output /data/benchmark/results.csv
    ;;
  select)
    python benchmark/select_best.py "$RESULTS" --policy "${2:-balanced}" --output benchmark/selected_stt.env
    ;;
  apply)
    [[ -f .env ]] || cp .env.example .env
    python benchmark/apply_selection.py --selection benchmark/selected_stt.env --target .env
    ;;
  all)
    "$0" run "$PROVIDERS"
    "$0" select balanced
    "$0" apply
    ;;
  *)
    cat <<'USAGE'
Usage:
  ./scripts/benchmark-stt.sh run [qwen,elevenlabs,soniox,deepgram]
  ./scripts/benchmark-stt.sh select [accuracy|balanced|latency]
  ./scripts/benchmark-stt.sh apply
  ./scripts/benchmark-stt.sh all [providers]
USAGE
    ;;
esac
