# 제3자 구성요소

이 저장소의 연결 코드와 UI는 `LICENSE`의 MIT 조건으로 제공됩니다. 모델, 모델 가중치, Python 패키지, 클라우드 API에는 각자의 별도 조건이 적용됩니다.

| 구성요소 | 용도 | 상위 프로젝트 |
|---|---|---|
| Qwen3-ASR | 로컬 스트리밍 STT | https://github.com/QwenLM/Qwen3-ASR |
| Qwen3-TTS | 공식 음성 복제 fallback | https://github.com/QwenLM/Qwen3-TTS |
| faster-qwen3-tts | CUDA graph 기반 오디오 스트리밍 | https://github.com/andimarafioti/faster-qwen3-tts |
| FastAPI | HTTP/WebSocket 서버 | https://github.com/fastapi/fastapi |
| Uvicorn | ASGI 서버 | https://github.com/encode/uvicorn |
| httpx | LLM/API HTTP 클라이언트 | https://github.com/encode/httpx |
| websockets | STT/TTS WebSocket 클라이언트 | https://github.com/python-websockets/websockets |
| PyYAML | 페르소나 설정 | https://github.com/yaml/pyyaml |
| faster-whisper | 선택적 STT 기준선 | https://github.com/SYSTRAN/faster-whisper |

Qwen 모델 저장소는 Apache-2.0으로 공개되어 있지만 실제 배포 시 사용한 모델 카드와 가중치 조건을 다시 확인하십시오. `faster-qwen3-tts` 연결 코드에는 해당 프로젝트의 MIT 조건이 적용됩니다. 클라우드 STT는 각 공급자의 상업 API 약관과 가격 정책이 적용됩니다.
