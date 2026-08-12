# 보안 및 개인정보

## 기본 방어

- 웹 UI는 Docker Compose에서 `127.0.0.1`에만 바인딩됩니다.
- API 키는 `.env`에서 gateway 서버로만 전달되고 브라우저 설정 응답에는 포함되지 않습니다.
- 업로드 파일 이름을 저장 경로로 직접 사용하지 않습니다.
- 목소리 프로필 ID는 제한된 문자 패턴만 허용합니다.
- 목소리 업로드 크기, 길이, 대본 길이를 제한합니다.
- 프로필 삭제 API를 제공합니다.
- 브라우저 마이크는 localhost 또는 HTTPS에서 사용합니다.

## 저장 위치

```text
data/voices/<profile_id>/reference.wav
data/voices/<profile_id>/metadata.json
data/benchmark/samples/
data/benchmark/manifest.csv
data/personas/<id>.yaml       # UI에서 생성한 텍스트 페르소나
```

ZIP 배포본에는 실제 음성을 넣지 않습니다.

페르소나 생성 API(`POST /api/personas`)는 별도 인증 없이 동작하는 trusted-local
기능입니다. 기본 Compose 포트는 `127.0.0.1`에만 바인딩되지만, 포트나 터널을 외부에
노출하면 누구나 대화 프롬프트를 추가할 수 있습니다. CORS 설정만으로 접근 제어가
되지 않으므로 외부 공개 전에는 인증·CSRF/Origin 정책·속도 제한을 추가해야 합니다.
기본 `gateway/personas`는 변경하지 못하며 생성 파일은 `/data/personas`(호스트의
`data/personas`)에 원자적으로 저장됩니다. 현재 수정·삭제·가져오기·소유권 기능은
범위에 포함하지 않습니다.

## 외부 전송

- `STT_MODE=qwen_ws`: 라이브 음성은 로컬 Qwen ASR로 전송됩니다.
- 클라우드 STT 모드: 라이브 음성이 선택 공급자에게 전송됩니다.
- 로컬 Ollama: 텍스트 대화가 로컬 호스트에 남습니다.
- 외부 OpenAI 호환 LLM URL: 대화 텍스트가 해당 서버로 전송됩니다.

각 서비스의 보관·학습·리전·삭제 정책은 계정 계약에서 별도로 확인해야 합니다.

## 공개 배포 전 필수 추가

현재 패키지는 개인 로컬 실행용입니다. 외부 네트워크에 노출하려면 다음이 필요합니다.

- 사용자 인증과 권한 분리
- TLS
- CSRF/Origin 정책 강화
- 요청 속도와 업로드 제한
- API 키 secret manager
- 음성 프로필 소유권 검사
- 감사 로그와 삭제 이력
- 합성 음성 표시 및 악용 신고
- 공급자별 개인정보 처리 고지

인증 없이 포트를 인터넷에 노출하지 마십시오.
