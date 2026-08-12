# Persona Duplex 시작 안내

이 패키지는 다음 경로를 한 프로그램으로 묶습니다.

```text
브라우저 마이크
  → 실시간 VAD와 오디오 프리롤
  → 스트리밍 STT
  → 페르소나 프롬프트가 적용된 스트리밍 LLM
  → 짧은 발화 단위 분할
  → Qwen3-TTS 사용자 음성 제로샷 복제
  → 브라우저의 겹침 재생·끼어들기·추임새
```

## 구현된 대화 동작

- AI가 말하는 동안에도 마이크를 계속 수신합니다.
- 사용자가 말을 시작하면 브라우저에서 AI 음량을 즉시 낮춥니다.
- 약 420ms 이상 계속 말하면 LLM 생성과 TTS 재생을 취소합니다.
- 짧은 “응”, “음”, “그렇구나”는 대화 중단이 아니라 추임새로 분류합니다.
- 사용자가 길게 말할 때 AI도 등록된 사용자 음성으로 낮은 음량의 짧은 추임새를 넣을 수 있습니다.
- 실제로 재생 완료된 문장만 대화 기록에 남깁니다. 끊긴 문장 전체를 들었다고 가정하지 않습니다.
- 참조 음성과 정확한 대본을 이용한 Qwen3-TTS full ICL 프롬프트를 캐시합니다.

이 구조는 ChatGPT Voice와 같은 독점형 종단간 음성 모델을 복제한 것이 아닙니다. 대신 STT·LLM·TTS를 독립적으로 바꿀 수 있는 모듈식 전이중 음성 파이프라인으로, 겹침·중단·추임새에 필요한 상태 제어를 직접 구현합니다.

## 준비물

- Windows 11 + Docker Desktop/WSL2 또는 Linux
- NVIDIA GPU와 최신 드라이버
- Docker Compose v2
- 로컬 LLM을 쓸 경우 Ollama
- Chrome 또는 Edge 권장
- 헤드폰 권장. 스피커 에코를 소프트웨어만으로 완전히 제거하는 것은 어렵습니다.

대략적인 운용 선택은 다음과 같습니다. GPU·드라이버·양자화·동시 모델 수에 따라 달라지므로 보장 수치는 아닙니다.

| 환경 | 권장 시작 모드 |
|---|---|
| GPU 없이 UI와 상태 머신만 검사 | 자동화 테스트용 `mock` |
| VRAM이 빠듯함 | 클라우드 STT + 로컬 Qwen TTS |
| 중간급 GPU, 로컬 우선 | Qwen3-ASR 0.6B `balanced` |
| 여유 있는 GPU, 정확도 우선 | Qwen3-ASR 1.7B `accuracy` |
| 실제 사용자 음성으로 후보 비교 완료 | `selected` |

로컬 ASR, Qwen TTS, Ollama `qwen3:1.7b`를 한 GPU에 올리는 구성을 기본 실전 경로로 사용합니다. 더 큰 LLM이 필요하면 `.env`의 모델을 바꾸되, 8GB VRAM에서는 모델 간 경쟁으로 warmup이 실패할 수 있습니다.

## Windows PowerShell 시작

압축을 푼 폴더에서 실행합니다.

```powershell
Copy-Item .env.example .env

.\persona-duplex.ps1 doctor
.\persona-duplex.ps1 start balanced
```

브라우저에서 다음 주소를 엽니다.

```text
http://localhost:8080
```

실행기는 Ollama `qwen3:1.7b`와 Qwen ASR/TTS 모델을 자동으로 다운로드·warmup하고, 세 모델이 준비된 뒤에만 UI 주소를 준비 완료로 표시합니다.

```powershell
# 로컬 균형 모드
.\persona-duplex.ps1 start balanced

# 로컬 정확도 모드
.\persona-duplex.ps1 start accuracy
```

더블클릭으로 전체 실행하고 `Ctrl+C`로 종료하려면 프로젝트 폴더의 `persona-duplex.bat`을 실행합니다. Docker Desktop이 꺼져 있으면 실행기가 먼저 시작하고, 기본 모드 `balanced`로 Ollama·ASR·TTS 모델이 준비될 때까지 자동으로 대기합니다. Compose 서비스는 재부팅 후 자동 시작하지 않으며, 종료 시 실행기가 `docker compose down --remove-orphans`로 해당 스택을 정리합니다. `mock`은 실전 실행 경로가 아닌 자동화 테스트용입니다.

기본 실행은 Tailscale Funnel로 외부 주소도 출력합니다. 원본 서버는 로컬 `127.0.0.1:8080`에 남고, 외부에는 게이트웨이 8080만 공개됩니다. `Ctrl+C` 또는 `persona-duplex.bat stop`을 실행하면 서비스와 Funnel이 함께 종료됩니다. 로컬에서만 쓰려면 `.env`에 `PUBLIC_TUNNEL=off`를 설정하세요. LocalTunnel은 `PUBLIC_TUNNEL=localtunnel`, Cloudflare Quick Tunnel은 `PUBLIC_TUNNEL=quick`으로 선택할 수 있습니다. 외부 터널이 `/api/health`를 통과하지 못하면 실행기는 실패하고 시작한 서비스도 정리합니다.

클라우드 STT를 쓸 때는 `.env`에 해당 키를 입력한 뒤 실행합니다.

```powershell
.\persona-duplex.ps1 start cloud-elevenlabs
.\persona-duplex.ps1 start cloud-soniox
.\persona-duplex.ps1 start cloud-deepgram
```

상태와 로그:

```powershell
.\persona-duplex.ps1 status
.\persona-duplex.ps1 logs
.\persona-duplex.ps1 stop
```

## Linux/WSL2 시작

```bash
cp .env.example .env

./persona-duplex.sh doctor
./persona-duplex.sh start balanced
```

## 사용자 목소리 등록

1. `http://localhost:8080`을 엽니다.
2. 화면의 기준 문장을 **실제 발화와 정확히 같게** 읽습니다. 화면의 `기준 음성 파일 선택`에서 기존 파일을 고를 수도 있습니다.
   업로드 허용 확장자는 `.webm`, `.wav`, `.mp4`, `.m4a`, `.ogg`, `.opus`이며, 파일 미리보기에서 선택한 파일명과 크기를 확인할 수 있습니다.
3. 녹음·업로드 모두 8~20초를 권장합니다. 한 사람만, 일정한 마이크 거리로 녹음하고, 대본과 실제 발화를 맞춥니다.
4. 본인 목소리 또는 명시적으로 허가받은 목소리임을 확인합니다.
5. `이 녹음으로 프로필 생성`을 누릅니다. 화면에 표시된 업로드 한도(기본 32MiB) 이하이어야 하며, 서버가 실제 오디오 디코드·길이·품질을 최종 확인합니다.
6. 모델 warm-up이 끝나면 `2. 실시간 대화`에서 기본 페르소나와 목소리를 선택해 대화를 시작합니다.
7. 필요하면 `새 페르소나 만들기`를 펼쳐 이름·짧은 설명·관계를 입력하고 직접 만든 페르소나를 저장합니다.
   생성 직후 `새 페르소나 선택`을 눌러야 다음 대화에 적용됩니다. 생성한 페르소나는 `data/personas`에
   보존되어 재시작 후에도 목록에 나타납니다.

대본은 5~1000자, 프로필 이름은 80자 이하이며 동의 확인이 필수입니다. 파일명은 서버에서
경로 요소를 제거해 기준 음성 저장 경로로 사용하지 않습니다. 브라우저의 `audio/*` MIME
표시는 참고용일 뿐이고, 확장자와 MIME이 일치하지 않아도 ffmpeg 디코드 결과가 최종 판단입니다.
빈 파일, 지원하지 않는 확장자, 화면에 표시된 업로드 한도 초과 파일, 3초 미만 또는 45초 초과
오디오는 등록되지 않습니다. 한도는 `VOICE_MAX_UPLOAD_BYTES`로 gateway와 Qwen TTS에 함께
전달하며 기본값은 33554432(32MiB)입니다. 등록 후 서버는 임시 업로드 원본을 정리하고
정규화된 24kHz mono WAV와 품질 진단만 프로필에 보존합니다.

목소리의 말투·높낮이까지 최대한 유지하려면 기준 음성을 최종 봇이 말하길 원하는 스타일로 녹음해야 합니다. 평평한 낭독을 넣고 감정적인 연기를 완전히 동일하게 요구하면 입력과 목표가 서로 모순됩니다.

## STT를 실제 사용자 음성으로 선발

1. Qwen3-ASR 1.7B 비교를 위해 먼저 `accuracy` 모드로 서버를 실행하고 `http://localhost:8080/benchmark`를 엽니다.
2. 12개 한국어 시험 문장 중 최소 10개를 같은 환경에서 녹음합니다.
3. 비교할 클라우드 서비스 키를 `.env`에 입력합니다.
4. 벤치마크를 실행합니다.

```powershell
.\benchmark-stt.ps1 run qwen,elevenlabs,soniox,deepgram
.\benchmark-stt.ps1 select balanced
.\benchmark-stt.ps1 apply
.\persona-duplex.ps1 start selected
```

Linux/WSL2:

```bash
./benchmark-stt.sh run qwen,elevenlabs,soniox,deepgram
./benchmark-stt.sh select balanced
./benchmark-stt.sh apply
./persona-duplex.sh start selected
```

선발 기준은 한국어 CER, 첫 부분 자막 지연, 발화 종료 후 최종 확정 지연입니다. 실패한 서비스와 키가 없는 서비스는 자동으로 제외됩니다.

## 페르소나 변경 및 생성

기본 예시는 `gateway/personas` 아래 YAML로 제공됩니다. 이 디렉터리는 이미지의
immutable 기본값이므로, 일반 사용자는 UI의 `새 페르소나 만들기`에서 생성하세요.
생성된 YAML은 `data/personas`에 저장되고 재시작 뒤에도 유지됩니다.

직접 기본 YAML을 수정하거나 새 파일을 추가할 수도 있지만, 컨테이너를 다시 빌드해야
반영됩니다. UI 생성의 필드와 제한은 [`docs/PERSONA_GUIDE_KO.md`](docs/PERSONA_GUIDE_KO.md)를
참조하세요.

- `default.yaml`: 개인 음성 비서
- `analyst.yaml`: 기술 분석가
- `friend.yaml`: 편안한 대화 친구
- `coach.yaml`: 실행 중심 코치
- `interviewer.yaml`: 질문 중심 인터뷰어

변경 후 gateway를 다시 시작합니다.

## 가장 먼저 확인할 문제

| 증상 | 확인 |
|---|---|
| 마이크 권한이 안 뜸 | `localhost` 또는 HTTPS인지 확인 |
| AI가 자기 소리에 반응함 | 헤드폰 사용, 마이크 입력 장치 확인, VAD 배수 상향 |
| 자주 끊김 | `VAD_END_MS_ASSISTANT`, `BARGE_IN_CONFIRM_MS` 상향 |
| 끼어들기가 느림 | `BARGE_IN_CONFIRM_MS`를 320~380ms로 낮춤 |
| 짧은 “응”에도 답변이 취소됨 | `SHORT_BACKCHANNEL_MAX_MS` 상향 |
| 첫 TTS가 오래 걸림 | 목소리 프로필 warm-up 확인, `TTS_CHUNK_SIZE=2` 시험 |
| 음색은 비슷한데 말투가 다름 | 원하는 말투로 참조 음성을 다시 녹음하고 정확한 대본 사용 |
| GPU 메모리 부족 | 클라우드 STT 사용 또는 0.6B ASR, LLM GPU 사용량 축소 |

상세 문서는 `docs` 폴더에 있습니다.
