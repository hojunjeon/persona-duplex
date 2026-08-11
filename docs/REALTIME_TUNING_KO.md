# 실시간 대화 튜닝

환경 변수는 `.env`에서 조정합니다.

## 끼어들기

| 변수 | 기본값 | 효과 |
|---|---:|---|
| `BARGE_IN_CONFIRM_MS` | 420 | 이 시간 이상 발화가 이어지면 실제 끼어들기로 확정 |
| `SHORT_BACKCHANNEL_MAX_MS` | 850 | 짧은 인정 반응으로 허용할 최대 길이 |
| `EMPTY_BACKCHANNEL_MAX_MS` | 320 | STT partial이 없을 때 잡음으로 무시할 최대 길이 |
| `PRE_ROLL_MS` | 440 | VAD 시작 직전 보존하는 오디오 |

끼어들기가 느리면 `BARGE_IN_CONFIRM_MS=340` 정도를 시험합니다. 기침과 키보드에도 자주 끊기면 500~650ms로 올립니다.

## 브라우저 VAD

| 변수 | 기본값 | 효과 |
|---|---:|---|
| `VAD_START_FRAMES` | 3 | 20ms 프레임이 연속으로 임계값을 넘을 때 발화 시작 |
| `VAD_END_MS_ASSISTANT` | 300 | AI 재생 중 무음이 이만큼 이어지면 사용자 발화 종료 |
| `VAD_END_MS_IDLE` | 520 | AI가 조용할 때 사용자 발화 종료 |
| `VAD_THRESHOLD_MIN` | 0.012 | 최소 RMS 임계값 |
| `VAD_NOISE_MULTIPLIER_ASSISTANT` | 3.4 | AI 재생 중 추정 소음 바닥 배수 |
| `VAD_NOISE_MULTIPLIER_IDLE` | 2.7 | 평상시 추정 소음 바닥 배수 |

AI가 자기 목소리에 반응하면 헤드폰을 먼저 사용합니다. 그다음 assistant multiplier를 4.0~5.0으로 올립니다. 사용자의 작은 목소리를 놓치면 최소 임계값 또는 idle multiplier를 조금 낮춥니다.

## AI 추임새

| 변수 | 기본값 |
|---|---:|
| `BACKCHANNEL_ENABLED` | true |
| `BACKCHANNEL_AFTER_MS` | 1900 |
| `BACKCHANNEL_COOLDOWN_MS` | 3300 |

추임새는 LLM이 즉석에서 긴 문장을 생성하지 않고 페르소나 YAML에 정의된 짧은 후보 중 하나를 Qwen TTS로 합성합니다. 사용자의 결론을 빼앗는 일을 줄이기 위한 제한입니다.

## LLM → TTS 절 분할

| 변수 | 기본값 |
|---|---:|
| `MAX_CLAUSE_CHARS` | 54 |
| `MIN_CLAUSE_CHARS` | 8 |
| `LLM_MAX_TOKENS` | 260 |

짧게 자르면 첫 음성이 빨라지지만 절 사이 운율이 끊길 수 있습니다. 한국어 대화에서는 35~60자 범위가 대체로 실용적입니다.

## Qwen TTS 청크

`TTS_CHUNK_SIZE`가 작을수록 첫 오디오 단위가 작아지지만 디코딩 오버헤드가 커집니다.

- `2`: 낮은 지연 우선
- `4`: 기본 균형
- `8`: 처리량·안정성 우선

`faster-qwen3-tts`가 로드되지 않고 공식 fallback을 사용하면 이 값만 바꿔도 진짜 오디오 스트리밍이 되지 않습니다. `/api/health`의 `loaded_backend`를 확인합니다.

## 체감 지연 읽기

UI의 세 값은 서로 다른 구간입니다.

- STT 확정: 사용자 발화 종료 → 최종 텍스트
- LLM 첫 토큰: 응답 작업 시작 → 첫 텍스트 토큰
- 첫 음성: 응답 작업 시작 → 첫 PCM 청크

총 체감 시간은 대략 STT 확정 + LLM 첫 토큰 + TTS 첫 음성 준비가 겹치거나 이어진 결과입니다. 현재 구조는 STT final 뒤 LLM을 시작하므로 STT 확정 지연이 가장 먼저 줄일 대상입니다.
