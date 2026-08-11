# STT 후보 조사와 기본 선택

조사 기준일은 2026-08-11입니다. 전체 후보와 공식 출처는 `benchmark/stt_landscape_2026-08.csv`에 있습니다.

## 평가 기준

음성 대화 봇에서 중요한 값은 녹음 파일 전체의 최종 정확도 하나가 아닙니다.

- 한국어 CER/WER
- 첫 부분 자막 도착 시간
- 사용자가 말을 끝낸 뒤 최종 텍스트가 확정되는 시간
- 짧은 발화와 코드 스위칭
- 스트리밍 세션 안정성
- 수동 finalize/commit 지원 여부
- 로컬 처리 가능성 및 개인정보
- GPU 메모리와 비용

## 런타임에 통합한 네 후보

### Qwen3-ASR 1.7B

정확도 우선 로컬 기본 후보입니다. Qwen의 공식 다국어 집계에서 1.7B는 CommonVoice 9.18 WER, Fleurs 4.90 WER로 공개되어 있고, 같은 표의 Whisper large-v3는 각각 10.77과 5.27입니다. 이 값은 한국어 단독 결과가 아니라 여러 언어의 집계이므로 사용자 한국어 음성에서의 승리를 보장하지 않습니다.

공식 저장소: https://github.com/QwenLM/Qwen3-ASR

### Qwen3-ASR 0.6B

GPU 메모리와 지연의 균형을 위한 로컬 후보입니다. 1.7B보다 공식 집계 정확도는 낮지만 Qwen TTS와 한 장의 GPU를 공유해야 할 때 현실적인 선택입니다.

### ElevenLabs Scribe v2 Realtime

수동 commit과 partial/committed transcript를 제공해 브라우저 VAD와 결합하기 쉽습니다. 업체는 약 150ms와 90개 이상 언어를 주장하지만, 이는 공급자 자료이므로 본 프로젝트는 그대로 우승 판정에 사용하지 않습니다.

공식 문서: https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/transcripts-and-commit-strategies

### Soniox stt-rt-v5

수동 finalize, keepalive, 다국어 힌트가 있는 실시간 후보입니다. 클라이언트 VAD가 한 번만 턴을 결정하도록 서버 endpoint detection을 끕니다.

공식 문서: https://soniox.com/docs/stt/rt/real-time-transcription

### Deepgram Nova-3

WebSocket streaming과 명시적 `Finalize`를 제공합니다. 공식 문서상 `from_finalize` 응답이 모든 상황에서 보장되는 것은 아니므로, 구현에는 1.25초 grace-time 뒤 최종 조각을 반환하는 fallback을 넣었습니다.

공식 문서: https://developers.deepgram.com/docs/finalize

## 조사했지만 기본 런타임에 넣지 않은 후보

- Google Cloud Chirp 3
- OpenAI transcription 계열
- Azure Speech to Text
- Amazon Transcribe
- AssemblyAI Whisper Streaming
- Fun-ASR-MLT-Nano
- SenseVoiceSmall
- Whisper large-v3
- faster-whisper
- sherpa-onnx
- GLM-ASR-Nano
- Alibaba DashScope Qwen3-ASR Realtime
- Vosk Korean

제외는 “나쁘다”는 뜻이 아닙니다. 초기 패키지에서 모든 공급자의 인증·SDK·세션 프로토콜을 유지하면 비교 도구보다 API 박물관이 되므로, 로컬 2개와 실시간 API 3개를 운영 후보로 좁혔습니다. faster-whisper와 OpenAI 배치 전사는 벤치마크 비교 경로를 남겼습니다.

## 자동 선발 방식

`benchmark/select_best.py`는 성공한 통합 런타임만 묶어 다음 통계를 계산합니다.

- 평균 CER
- 중앙 CER
- 발화 종료 후 확정 지연 p90
- 첫 부분 자막 지연 p90
- 평균 RTF

정책:

- `accuracy`: CER 우선
- `latency`: 발화 종료 후 확정 지연 우선
- `balanced`: CER에 지연 패널티를 더함

균형 점수의 개념식:

```text
mean_CER
+ 0.08 × min(p90_final_ms, 5000) / 1000
+ 0.025 × min(p90_partial_ms, 5000) / 1000
```

기본 지연 상한은 1200ms입니다. 상한을 만족하는 후보가 없으면 성공한 후보 전체에서 선택합니다.

## 기본 권장

- 충분한 GPU와 로컬 처리 우선: Qwen3-ASR 1.7B
- 한 GPU에서 TTS와 공유: Qwen3-ASR 0.6B
- VRAM을 TTS에 집중하거나 낮은 네트워크 지연 지역: ElevenLabs/Soniox/Deepgram을 실제 녹음으로 비교
- 최종 운영: `/benchmark`에서 본인 음성 10~12개를 녹음하고 `selected` 모드 사용
