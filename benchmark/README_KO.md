# STT 벤치마크 폴더

## 파일

- `stt_landscape_2026-08.csv`: 조사한 18개 STT 서비스·모델과 공식 출처
- `benchmark_stt.py`: 동일 오디오를 각 후보에 보내 CER/WER와 지연 측정
- `metrics.py`: 한국어 정규화와 편집 거리
- `select_best.py`: 통합된 실시간 후보 중 정책별 자동 선발
- `apply_selection.py`: 선발 결과를 `.env`에 병합
- `manifest.example.csv`: 수동 manifest 예시
- `selected_stt.env`: 벤치마크 실행 후 생성되는 런타임 설정

## 권장 경로

```text
/benchmark 웹 화면에서 녹음
  → data/benchmark/manifest.csv
  → benchmark_stt.py
  → data/benchmark/results.csv
  → select_best.py
  → benchmark/selected_stt.env
  → apply_selection.py
  → persona-duplex start selected
```

## 직접 실행

```bash
python benchmark/benchmark_stt.py \
  --manifest data/benchmark/manifest.csv \
  --providers qwen,elevenlabs,soniox,deepgram \
  --output data/benchmark/results.csv

python benchmark/select_best.py data/benchmark/results.csv --policy balanced
python benchmark/apply_selection.py
```

호스트 Python 의존성 설치가 번거로우면 `scripts/benchmark-stt.sh` 또는 `scripts\benchmark-stt.ps1`을 사용합니다. 측정 단계가 Docker 컨테이너에서 실행됩니다.

## 공급자 이름

- `qwen`: 실행 중인 bundled Qwen3-ASR WebSocket
- `elevenlabs`: Scribe v2 Realtime
- `soniox`: stt-rt-v5
- `deepgram`: Nova-3
- `faster-whisper`: 선택적 로컬 배치 기준선
- `openai`: 선택적 배치 전사 기준선

`faster-whisper`와 `openai`는 현재 자동 런타임 선발 대상이 아니라 비교용입니다.
