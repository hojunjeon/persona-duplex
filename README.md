# Persona Duplex

사용자 목소리를 Qwen3-TTS로 제로샷 복제하고, 스트리밍 STT와 페르소나 LLM을 연결해 전이중에 가까운 음성 대화를 제공하는 로컬 우선 프로젝트입니다.

## 주요 기능

- 브라우저 AudioWorklet 기반 16kHz PCM 마이크 스트리밍
- AI 재생 중에도 마이크를 유지하는 full-duplex 입력 경로
- 로컬 즉시 duck과 서버 확정 취소를 분리한 barge-in
- 짧은 사용자 추임새와 실제 중단 발화 구분
- 별도 저음량 오디오 채널의 AI 추임새
- Qwen3-ASR 0.6B/1.7B 로컬 스트리밍
- ElevenLabs, Soniox, Deepgram 실시간 STT 어댑터
- OpenAI 호환 스트리밍 LLM 엔드포인트. 기본값은 Ollama `qwen3:1.7b`
- Qwen3-TTS full ICL 음성 프롬프트 캐시
- `faster-qwen3-tts` 진짜 오디오 청크 스트리밍, 공식 `qwen-tts` 전체 절 fallback
- 본인 음성 기반 한국어 STT CER·지연 벤치마크와 자동 선발
- Docker Compose, Windows PowerShell, Linux/WSL2 실행기

## 디렉터리

```text
gateway/                 브라우저 UI, 대화 상태 머신, STT/LLM/TTS 어댑터
services/qwen_asr/       Qwen3-ASR 스트리밍 WebSocket 서비스
services/qwen_tts/       음성 등록, prompt cache, 스트리밍 TTS 서비스
benchmark/               STT 후보 조사·측정·자동 선발
scripts/                 실제 실행·벤치마크 스크립트
*.bat                    Windows용 시작·Docker 켜기·Docker 끄기 버튼
gateway/personas/        페르소나 YAML
data/voices/             등록한 참조 음성. ZIP에는 비어 있음
data/benchmark/          사용자 STT 시험 녹음과 결과
docs/                    설계·튜닝·보안·한계 문서
```

## 빠른 실행

```bash
cp .env.example .env
./scripts/persona-duplex.sh doctor
./scripts/persona-duplex.sh start balanced
```

Windows:

```powershell
Copy-Item .env.example .env
.\scripts\persona-duplex.ps1 doctor
.\scripts\persona-duplex.ps1 start balanced
```

더블클릭 실행기:

```text
start.bat
docker-on.bat
docker-off.bat
```

`docker-on.bat`으로 Docker Desktop을 먼저 켠 뒤 `start.bat`을 실행합니다. 기본 모드는 `balanced`이며, `Ctrl+C`는 Persona Duplex 서비스만 멈추고 Docker Desktop은 그대로 둡니다. 실행할 때마다 이미지를 강제로 다시 만들지 않으며, 소스가 바뀌어 다시 만들 필요가 있을 때만 `./scripts/persona-duplex.ps1 build`를 실행합니다. `accuracy`, `selected`, `cloud-elevenlabs`, `cloud-soniox`, `cloud-deepgram`은 `start.bat`의 첫 번째 인자로 선택할 수 있습니다.

실행기 기본값은 Tailscale Funnel(`PUBLIC_TUNNEL=tailscale`)이며, 준비가 끝나면 Tailscale 도메인 외부 주소를 출력합니다. 원본 서버는 계속 `127.0.0.1:8080`에만 바인딩되고, 외부에는 게이트웨이 8080만 공개됩니다. 외부 접속을 계속 사용하려면 `.env`의 `PUBLIC_TUNNEL`을 끄지 마세요. LocalTunnel은 `PUBLIC_TUNNEL=localtunnel`, Cloudflare Quick Tunnel은 `PUBLIC_TUNNEL=quick`으로 선택할 수 있습니다. 선택한 터널이 `/api/health`를 통과하지 못하면 실행기는 실패하고 시작한 서비스도 정리합니다.

UI는 `http://localhost:8080`, STT 녹음 벤치마크는 `http://localhost:8080/benchmark`입니다.

## 중요한 한계

- 제로샷 복제는 참조 음성의 화자 정체성과 운율 단서를 조건으로 사용하지만, 임의의 문장에서 목소리·말투·감정·높낮이를 수학적으로 완전히 동일하게 보장하지 않습니다.
- 모듈식 STT→텍스트 LLM→TTS 구조이므로 종단간 speech-to-speech 모델보다 감정 연속성이 약할 수 있습니다.
- 공식 `qwen-tts` fallback은 절 전체를 생성한 뒤 전송합니다. 낮은 TTFA가 필요하면 `faster-qwen3-tts`가 정상 로드되어야 합니다.
- 스피커 출력이 마이크로 다시 들어오는 환경에서는 헤드폰 없이 안정적인 겹침 대화가 어렵습니다.
- “최고 STT”는 업체 표로 확정하지 않습니다. 사용자 음성·마이크·네트워크로 제공된 벤치마크를 실행해 선발합니다.

## 문서

- `START_HERE_KO.md`
- `docs/ARCHITECTURE_KO.md`
- `docs/STT_RESEARCH_KO.md`
- `docs/STT_BENCHMARK_KO.md`
- `docs/REALTIME_TUNING_KO.md`
- `docs/PERSONA_GUIDE_KO.md`
- `docs/VOICE_CLONE_LIMITS_KO.md`
- `SECURITY.md`
- `TEST_REPORT.md`
