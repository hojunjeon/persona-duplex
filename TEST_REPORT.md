# Persona Duplex 검증 보고서

검증일: 2026-08-11

## 1. 구현 범위

이 패키지는 다음 경로를 실제 코드로 연결합니다.

```text
브라우저 마이크
  → AudioWorklet PCM16 16 kHz / 20 ms
  → 브라우저 RMS VAD와 서버 pre-roll
  → Qwen3-ASR 또는 실시간 상용 STT
  → 페르소나 프롬프트가 적용된 스트리밍 LLM
  → 짧은 한국어 발화 단위 분할
  → Qwen3-TTS 사용자 음성 제로샷 복제
  → 두 개의 WebAudio 재생 채널
```

구현된 대화 제어는 다음과 같습니다.

- AI 재생 중에도 마이크 입력을 유지합니다.
- 브라우저가 발화를 감지하면 서버 왕복 전에 주 음성을 즉시 duck합니다.
- 일정 시간 이상 이어지는 사용자 발화는 LLM 작업, TTS 작업, 브라우저 재생을 함께 취소합니다.
- 짧은 `응`, `음`, `아하`와 실제 끼어들기를 분리합니다.
- 사용자의 긴 발화 중에는 별도 저음량 채널로 짧은 AI 추임새를 재생할 수 있습니다.
- 중간에 끊긴 TTS 절은 대화 기록에 완전히 들린 문장으로 저장하지 않습니다.
- 첫 STT 확정이 늦는 동안 다음 발화가 시작되는 경우 오디오를 별도 버퍼에 보존하고 순서대로 확정합니다.
- 더 최신 사용자 발화가 있으면 이전 발화가 낡은 답변을 시작하지 않도록 억제합니다.

## 2. STT 구현과 선발 방식

통합 런타임 후보:

1. Qwen3-ASR 1.7B, 로컬 정확도 우선
2. Qwen3-ASR 0.6B, 로컬 GPU 균형
3. ElevenLabs Scribe v2 Realtime
4. Soniox stt-rt-v5
5. Deepgram Nova-3

비교 조사표에는 총 18개 로컬·오픈소스·상용 후보가 들어 있습니다. 런타임에 넣지 않은 서비스도 공식 출처와 제외 이유를 `benchmark/stt_landscape_2026-08.csv`에 기록했습니다.

`/benchmark` 화면에서 같은 사용자가 같은 마이크로 한국어 문장 10~12개를 녹음한 뒤 다음 값을 같은 파일에서 측정합니다.

- CER, WER
- 첫 부분 자막 지연
- 발화 종료 후 최종 확정 지연
- 전체 시간과 RTF
- 공급자 오류

`accuracy`, `latency`, `balanced` 정책으로 성공한 실시간 후보 중 하나를 고르고 `benchmark/selected_stt.env`를 생성합니다. 공급자 자체 광고 수치만 보고 우승자를 정하지 않습니다.

## 3. Qwen TTS 음성 복제 구현

등록 입력:

- 사용자 참조 음성 3~45초, 권장 8~20초
- 오디오에서 실제로 읽은 정확한 대본
- 본인 음성 또는 명시적 허가 확인

두 backend를 지원합니다.

- `faster-qwen3-tts`: `generate_voice_clone_streaming(text, language, ref_audio, ref_text, chunk_size)` 공개 API를 사용해 PCM 청크를 생성합니다.
- 공식 `qwen-tts`: `create_voice_clone_prompt(..., x_vector_only_mode=False)`로 reusable full-ICL prompt를 캐시하고 절 전체 생성 fallback을 사용합니다.

참조 음성은 ffmpeg로 24 kHz mono PCM WAV로 정규화하고, 길이·RMS·클리핑·활성 음성 비율·DC offset을 검사합니다.

## 4. 자동 검증 결과

- Python compileall 통과
- Pytest 22개 통과
- 브라우저 UI JavaScript 문법 통과
- AudioWorklet 문법 통과
- 벤치마크 페이지 inline JavaScript 문법 통과
- Docker Compose YAML 파싱 통과, 4개 서비스 확인
- Bash 실행기 문법 통과
- STT 조사 CSV 18개 행과 스키마 확인
- 실제 mock gateway 프로세스를 띄워 `/api/config`, `/api/health` 응답 확인
- mock WebSocket 대화와 binary audio frame 테스트 통과
- Qwen3-ASR in-place streaming state 계약 테스트 통과
- 두 Qwen TTS backend의 서로 다른 공개 API 계약 테스트 통과
- 연속 발화, 낡은 답변 억제, 짧은 추임새 경쟁 조건 테스트 통과
- ElevenLabs의 빈 commit packet 방지 테스트 통과
- CER/WER, 후보 집계, 자동 선발, `.env` 적용 테스트 통과
- 임의의 실제 키 형태가 저장소에 들어 있지 않은지 검사 통과

## 5. 이 환경에서 실행하지 못한 항목

현재 제작 컨테이너에는 Docker CLI/daemon, PowerShell, NVIDIA GPU, 사용자 API 키가 없습니다. 따라서 다음은 성공했다고 꾸며 적지 않았습니다.

- Qwen3-ASR 실제 가중치 다운로드와 GPU 추론
- Qwen3-TTS 실제 가중치 다운로드와 사용자 음성 합성
- Docker image build
- PowerShell 실제 실행
- ElevenLabs·Soniox·Deepgram의 사용자 계정 실시간 호출
- 사용자 음성 기반 공급자별 CER/지연 최종 순위

이 항목은 대상 NVIDIA 머신에서 `doctor → mock → accuracy/balanced → /benchmark → selected` 순서로 검증하도록 실행기와 측정 도구를 포함했습니다.

## 6. 품질 한계

이 프로젝트는 독점 종단간 speech-to-speech 모델의 내부 구조를 복제한 것이 아닙니다. STT, 텍스트 LLM, TTS를 병렬 상태 머신으로 연결한 모듈식 전이중 시스템입니다. 따라서 끼어들기와 추임새는 구현되어도, 종단간 모델처럼 사용자 음성의 감정·호흡을 입력에서 응답까지 연속적으로 보존하지는 않습니다.

Qwen3-TTS 제로샷 복제는 음색, 기본 음역, 리듬과 기준 녹음의 스타일을 조건으로 사용하지만 임의의 모든 문장에서 사용자의 피치 곡선·말버릇·감정·높낮이를 완전히 동일하게 보장하지 않습니다. 최종 봇이 쓸 톤으로 기준 음성을 녹음하고, 같은 문장과 같은 음성으로 여러 backend를 블라인드 비교해야 합니다.

## 7. 대상 머신에서의 합격 기준

다음 조건을 만족하면 실제 운용 후보로 판단할 수 있습니다.

- 참조 음성 등록과 warm-up 성공
- 10개 이상 한국어 STT 샘플에서 선택 정책에 맞는 후보 확정
- 발화 종료 후 STT p90 지연이 목표 범위 안에 있음
- 사용자 끼어들기 시 주 음성이 즉시 낮아지고 1초 안에 재생 큐가 비워짐
- 짧은 `응`에 답변이 과도하게 취소되지 않음
- 20회 이상 연속 대화에서 이전 발화가 낡은 답변을 시작하지 않음
- 장문·숫자·영어 고유명사에서 음색과 발음이 허용 범위에 있음
- 스피커 대신 헤드폰 환경에서 자기 음성 오감지가 안정적으로 억제됨
