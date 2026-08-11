# 아키텍처

## 전체 경로

```text
┌──────────────── Browser ────────────────┐
│ Microphone                              │
│   ↓ AudioWorklet, PCM16 16 kHz / 20 ms │
│ Local RMS VAD                           │
│   ├─ speech_start → 즉시 duck          │
│   └─ speech_end                         │
│                                         │
│ WebAudio playback                       │
│   ├─ channel 1: 주 답변                 │
│   └─ channel 2: 낮은 음량 추임새        │
└────────────────┬────────────────────────┘
                 │ WebSocket
┌────────────────▼ Gateway ───────────────┐
│ pre-roll buffer                         │
│ barge-in confirmation / cancellation    │
│ streaming STT adapter                   │
│ persona prompt + conversation history   │
│ streaming LLM                           │
│ clause buffer                           │
│ streaming TTS adapter                   │
│ heard-only history commit               │
└───────┬───────────────────────┬──────────┘
        │                       │
┌───────▼────────┐      ┌───────▼──────────┐
│ Qwen3-ASR      │      │ Qwen3-TTS        │
│ or cloud STT   │      │ user voice cache │
└────────────────┘      └──────────────────┘
```

## 왜 단순 턴제가 아닌가

대화 중 겹침을 만들기 위해 입력과 출력의 생명주기를 분리합니다.

1. AI 오디오가 재생 중이어도 마이크 스트림을 닫지 않습니다.
2. 브라우저 VAD가 발화를 감지하면 서버 응답을 기다리지 않고 main gain을 낮춥니다.
3. 서버는 짧은 사용자 반응인지 실제 끼어들기인지 약 420ms 동안 관찰합니다.
4. 실제 끼어들기면 LLM producer와 TTS consumer 작업을 취소하고 브라우저 재생 큐를 비웁니다.
5. “응”, “음” 같은 짧은 반응이면 오디오를 다시 원래 음량으로 복구하고 기존 답변을 계속합니다.
6. 사용자가 길게 말하면 독립된 backchannel TTS가 낮은 음량 채널로 짧게 반응할 수 있습니다.

## 발화 상태

```text
idle → listening → thinking → speaking
 ↑         │           │          │
 └─────────┴───────────┴──────────┘
       empty / cancel / playback idle
```

`assistant_active`와 `assistant_playing`은 구분합니다.

- `assistant_active`: LLM 또는 TTS 생성 작업이 살아 있음
- `assistant_playing`: 브라우저에 주 음성 스트림이 재생되고 있음

첫 음성이 아직 나오지 않았어도 사용자가 새로 말하면 생성 작업을 취소할 수 있습니다.

## 재생 기록의 일관성

각 TTS 절은 `stream_id`를 갖습니다. 브라우저는 해당 절의 모든 PCM이 실제 재생 완료된 경우에만 `audio.played`를 보냅니다. 서버는 재생 완료된 절만 assistant history에 넣습니다. 끼어들기로 절의 중간이 잘렸을 때 생성된 전체 텍스트를 사용자가 들었다고 기록하는 오류를 피합니다.

## LLM 스트리밍과 TTS 병렬화

LLM 토큰은 `ClauseBuffer`에 들어갑니다. 문장부호 또는 최대 글자 수에서 짧은 절을 방출합니다.

```text
LLM token stream ──> clause queue(max 3) ──> Qwen TTS ──> PCM chunks
```

queue 크기를 제한해 LLM이 TTS보다 지나치게 앞서 생성하지 않도록 backpressure를 겁니다. 끼어들기 시 이미 수십 문장을 생성해 비용과 지연을 낭비하는 일을 줄입니다.

## 음성 복제 경로

등록 시:

```text
WebM/MP4/WAV
  → ffmpeg 24 kHz mono PCM WAV
  → 길이·RMS·클리핑·무음 비율 검사
  → reference.wav + exact transcript
  → full ICL 음성 조건
  → 공식 backend: reusable voice_clone_prompt LRU cache
  → faster backend: ref_audio/ref_text 기반 내부 reference cache warm-up
```

합성 시 공식 `qwen-tts` backend는 캐시된 prompt object를 사용합니다. `faster-qwen3-tts` backend는 공개 API에 맞춰 `ref_audio`와 `ref_text`를 전달하며, 등록 직후 짧은 warm-up 생성을 수행해 backend의 lazy reference 준비 비용을 앞당깁니다.

## 데이터 흐름과 개인정보

- Qwen ASR 모드: STT 오디오는 로컬 컨테이너 안에서 처리됩니다.
- 클라우드 STT 모드: 라이브 마이크 PCM이 선택한 공급자에게 전송됩니다.
- Qwen TTS: 참조 음성과 합성은 로컬 컨테이너에서 처리됩니다.
- LLM: 기본 Ollama는 로컬 호스트입니다. 다른 OpenAI 호환 URL을 넣으면 텍스트 대화가 해당 서버로 전송됩니다.
