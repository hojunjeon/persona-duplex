# 사용자 음성 기반 STT 벤치마크

## 1. 시험 녹음 만들기

Persona Duplex를 실행한 뒤 다음 주소를 엽니다.

```text
http://localhost:8080/benchmark
```

12개 문장을 같은 마이크·거리·방에서 평소 말투로 읽습니다. 최소 10개를 권장합니다. 파일과 정답은 다음 위치에 저장됩니다.

```text
data/benchmark/samples/
data/benchmark/manifest.csv
```

시험 문장은 숫자, 받침, 영어 기술어, 짧은 추임새, 긴 문장, 끼어들기 표현을 섞었습니다.

## 2. 후보 실행 준비

Qwen 후보를 포함하려면 로컬 ASR 서비스를 먼저 실행합니다. 자동 선발의 Qwen 운영값은 정확도 우선 1.7B이므로 공정하게 비교하려면 `accuracy` 모드로 측정합니다.

```bash
./scripts/persona-duplex.sh start accuracy
```

클라우드 후보를 포함하려면 `.env`에 필요한 키를 넣습니다.

```dotenv
ELEVENLABS_API_KEY=
SONIOX_API_KEY=
DEEPGRAM_API_KEY=
OPENAI_API_KEY=
```

## 3. 측정

Linux/WSL2:

```bash
./scripts/benchmark-stt.sh run qwen,elevenlabs,soniox,deepgram
```

Windows PowerShell:

```powershell
.\scripts\benchmark-stt.ps1 run qwen,elevenlabs,soniox,deepgram
```

결과는 `data/benchmark/results.csv`에 저장됩니다.

열 설명:

| 열 | 의미 |
|---|---|
| `transcript` | 공급자가 확정한 텍스트 |
| `first_partial_ms` | 전송 시작부터 첫 부분 자막까지 |
| `final_after_end_ms` | 마지막 오디오 전송 뒤 확정까지 |
| `total_ms` | 전체 호출 시간 |
| `rtf` | 처리 시간 / 오디오 길이 |
| `cer` | 정규화된 문자 오류율 |
| `wer` | 공백 단위 단어 오류율 |
| `error` | 실패 원인 |

스트리밍 후보는 기본적으로 오디오를 실제 시간 속도로 20ms씩 전송합니다. `--no-pace`는 서버 처리량 실험용이며 대화 체감 지연 비교에는 사용하지 않는 편이 맞습니다.

## 4. 선발

```bash
./scripts/benchmark-stt.sh select accuracy
./scripts/benchmark-stt.sh select balanced
./scripts/benchmark-stt.sh select latency
```

Windows에서는 같은 인자를 `scripts\benchmark-stt.ps1`에 사용합니다.

출력 파일:

```text
benchmark/selected_stt.env
```

## 5. 적용

```bash
./scripts/benchmark-stt.sh apply
./scripts/persona-duplex.sh start selected
```

적용 스크립트는 `.env`의 기존 LLM·TTS·페르소나 설정을 보존하고 STT 관련 값만 갱신합니다.

## 공정한 비교 조건

- 같은 녹음 파일을 모든 후보에 사용합니다.
- 클라우드 후보는 같은 시간대와 네트워크에서 반복 측정합니다.
- 한 문장 한 번의 결과로 결정하지 않습니다.
- API 키 누락, rate limit, 네트워크 실패는 정확도 100% 오류로 채우지 않고 실패 행으로 분리합니다.
- 공급자별 비용은 CER와 별도로 판단합니다.
- 음성 대화에서는 최종 CER가 약간 낮아도 `final_after_end_ms`가 지나치게 크면 체감 품질이 나쁠 수 있습니다.
