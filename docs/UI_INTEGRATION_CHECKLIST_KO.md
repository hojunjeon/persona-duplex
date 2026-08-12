# Persona Duplex UI 통합 체크리스트

이 문서는 개편된 정적 화면이 기존 Gateway API와 WebSocket 세션에 실제로 연결됐는지 확인하기 위한 실행 문서다. 현재 구현과 런타임 증거가 확정되지 않은 항목은 의도적으로 `TODO` 또는 `UNVERIFIED`로 둔다. `tests/tunnel_fallback_harness.ps1`는 기존 사용자 변경으로 취급하며 이 문서나 구현에서 수정하지 않는다.

## 조사 기준

- 화면: `gateway/app/static/index.html`, `gateway/app/static/style.css`, `gateway/app/static/app.js`
- HTTP: `GET /api/config`, `GET/POST /api/personas`, `GET /api/voices`, `POST /api/voices/enroll`, `PATCH/DELETE /api/voices/{profile_id}`, `POST /api/voices/{profile_id}/warmup`, `GET /api/health`
- WebSocket: `/ws/conversation`; `session.ready`, `session.configure`, `session.configured`, `session.state`, `transcript.*`, `assistant.*`, `audio.*`, `metrics.*`, `warning`, `error`
- 서버 흐름: `gateway/app/main.py`, `gateway/app/session.py`
- 정적/계약 테스트: `tests/test_integration_contracts.py`, `tests/test_persona_api.py`, `tests/test_voice_enroll_upload.py`

## 체크리스트

| ID | 대상 화면/기능 | 구현 조건 | 검증 명령 / 실제 동작 시나리오 | 증거 위치 | 상태 |
|---|---|---|---|---|---|
| UI-INT-001 | 초기화·런타임 상태 | `/api/config` 응답으로 런타임 정보, 업로드 제한, 페르소나 목록을 채우고 `/api/voices` 결과로 목소리 선택지를 채운다. API 실패는 화면에 오류로 남기며 하드코딩된 선택 상태를 성공으로 표시하지 않는다. | `python -X utf8 -m pytest -q tests/test_integration_contracts.py -k 'static or config'`; 브라우저에서 새로고침 후 Network의 `/api/config`, `/api/personas`, `/api/voices` 200과 선택 목록/런타임 문구 확인 | `gateway/app/static/app.js` `loadConfig`, `refreshPersonas`, `refreshVoices`; `gateway/app/main.py` public routes | UNVERIFIED |
| UI-INT-002 | 내비게이션·관리 드로어 | 라이브/목소리/페르소나 내비게이션이 해당 앵커로 이동하고 현재 위치의 `active`/`aria-current`가 갱신된다. 존재하지 않는 `#settings`로 이동하는 dead link은 제거하거나 실제 설정 대상과 연결하고, 대화 중 관리 조작은 안전하게 차단한다. | 브라우저에서 네 개 내비게이션을 각각 클릭해 URL hash, 스크롤 대상, active 표시를 확인; 대화 중 목소리·페르소나 조작 시 차단 문구 확인 | `gateway/app/static/index.html` nav; `gateway/app/static/app.js` `bindEvents`; `#settings` 대상 부재가 현재 결손 | UNVERIFIED |
| UI-INT-003 | 선택 목소리 요약 | `voiceSelect`의 실제 선택 프로필 이름/메타데이터가 사이드바 요약에 반영되고, 새로고침·등록·수정·삭제 후에도 선택 상태와 요약이 일치한다. 빈 목록은 등록 안내로 표시한다. | mock API 또는 실행 스택에서 프로필 선택→목록 새로고침→등록/수정/삭제를 수행하고 요약 텍스트·select 값을 비교 | `index.html` 선택 요약 영역; `app.js` `selectVoice`, `refreshVoices` | UNVERIFIED |
| UI-INT-004 | 현재 페르소나 요약 | `personaSelect`의 실제 이름·정체성·관계가 사이드바 요약에 반영되고, 생성 후 기본 선택을 보존한다. 생성한 페르소나를 명시적으로 선택하면 다음 `session.configure`에 그 ID를 보낸다. | 브라우저에서 페르소나 생성→현재 선택 유지→새 페르소나 선택→대화 시작; WebSocket `session.configure.persona_id`와 요약 문구 대조 | `index.html` persona summary/create panel; `app.js` `renderPersonas`, `submitPersona`, `selectPersona`, `handleServerMessage` | UNVERIFIED |
| UI-INT-005 | 라이브 세션 시작/종료 | 시작 전 목소리·페르소나 선택을 검증하고 WebSocket 연결→마이크 권한→`session.configure` 순서를 유지한다. 종료/연결 실패/페이지 이탈에서 마이크, 오디오 큐, 버튼, 관리 컨트롤이 원상 복구된다. | `python -X utf8 -m pytest -q tests/test_integration_contracts.py -k websocket`; 브라우저에서 시작/권한 거부/종료를 각각 실행하고 WS close 및 버튼 상태 확인 | `app.js` `startConversation`, `stopConversation`, `onclose`; `session.py` `_configure` | BLOCKED |
| UI-INT-006 | 라이브 스테이지·세션 타이머 | `session.state`와 `assistant.*`/`audio.*` 이벤트가 상태 문구, orb/pill 시각 상태, LIVE 표시, 세션 경과 시간에 반영된다. 정지/idle 시 타이머를 멈추고 초기화한다. | 실제 mock 또는 구성된 런타임에서 사용자 발화→thinking→speaking→idle→stop을 수행하고 각 이벤트 직후 문구·CSS 상태·타이머를 확인 | `index.html` `.session-timer`, `.audio-orb`, `.listening-pill`; `app.js` `handleServerMessage`, `setStatus`; `session.py` `_set_state` | UNVERIFIED |
| UI-INT-007 | 전이중 오디오·barge-in | PCM 마이크가 AudioWorklet을 통해 전송되고, VAD `client.speech_start/end`, `audio.duck/unduck/stop`, `assistant.interrupted`, `audio.played`, `assistant.playback_idle`가 UI/플레이어와 일치한다. 중간 절을 들은 것으로 커밋하지 않는다. | 헤드폰 환경에서 AI 응답 중 짧은 추임새와 긴 끼어들기를 각각 말해 duck 후 복귀/응답 중단을 확인; 브라우저 console과 WS 이벤트 순서를 캡처 | `app.js` `processMicPacket`, `DuplexPlayer`, `handleServerMessage`; `session.py` barge-in/played 경로 | BLOCKED |
| UI-INT-008 | 실시간 지표·대화 로그 | `transcript.partial/final/empty`, `assistant.turn_start/delta`, `metrics.*`, `warning/error`를 로그·지표에 표시하고, 연결 종료 후에도 마지막 오류/경고를 숨기지 않는다. 지표가 없을 때 `-`를 유지한다. | 실제 발화 1회와 오류/빈 발화 1회를 실행해 로그·STT/LLM/첫 음성 지표와 서버 이벤트가 일치하는지 확인 | `app.js` `appendBubble`, `updateMetrics`, `handleServerMessage`; `session.py` event sends | UNVERIFIED |
| UI-INT-009 | 음성 등록·프로필 lifecycle | 녹음과 파일 첨부가 서로 다른 제출 경로로 실제 선택 파일을 전송하고, 동의/확장자/크기 오류를 표시한다. 등록→warmup→목록 반영→선택, 수정→warmup, 삭제까지 UI 상태가 API 결과와 일치한다. | `python -X utf8 -m pytest -q tests/test_integration_contracts.py -k voice`; 브라우저에서 녹음/파일 각각 등록하고 warmup 완료·수정·삭제를 확인 | `app.js` `enrollVoice`, `editVoice`, `deleteVoice`; `main.py` voice routes; `tests/test_voice_enroll_upload.py` | BLOCKED |
| UI-INT-010 | 페르소나 생성·선택 lifecycle | 기본 3필드와 고급 필드를 API canonical payload로 전송하고 validation/conflict 오류를 표시한다. 생성 직후 기존 선택은 보존하며 명시적 선택 뒤에만 세션 대상이 바뀐다. | `python -X utf8 -m pytest -q tests/test_persona_api.py -k 'create or static or websocket'`; 브라우저에서 정상/중복/잘못된 입력 시나리오 실행 | `app.js` `submitPersona`; `main.py` `/api/personas`; `tests/test_persona_api.py` | UNVERIFIED |
| UI-INT-011 | 회귀·증거 기록 | 변경 후 관련 정적 계약/서버 테스트를 통과시키고, 가능한 경우 실제 브라우저 런타임에서 위 시나리오의 Network/Console/WS 또는 화면 캡처 경로를 기록한다. 환경상 실행할 수 없는 항목은 추정하지 않고 `UNVERIFIED`/`BLOCKED` 사유를 남긴다. | `python -X utf8 -m pytest -q tests/test_integration_contracts.py tests/test_persona_api.py tests/test_voice_enroll_upload.py`; 런타임 불가 시 명령·오류를 그대로 기록 | 이 문서의 각 항목 증거란 및 리뷰/검증 worker 보고서 | UNVERIFIED |

## 독립 리뷰 재작업 게이트

독립 코드·테스트 리뷰에서 다음 결함이 재현되어 구현 worker의 재작업 대상으로 고정했다. 이 항목들은 수정과 재리뷰가 끝날 때까지 `TODO`이며, 최종 검증 worker는 재리뷰 `PASS` 뒤에만 생성한다.

| ID | 대상 | 재작업 조건 | 검증 | 상태 |
|---|---|---|---|---|
| UI-REVIEW-001 | 페르소나 관계 표시 | `GET /api/personas`와 `Persona.summary()`가 `relationship`을 반환하거나, API가 의도적으로 요약에서 제외된다면 UI가 그 계약을 따르도록 정렬한다. 생성·선택 후 사이드바 요약에 실제 관계/정체성이 표시되어야 한다. | `python -X utf8 -m pytest -q tests/test_persona_api.py -k 'static or create'`; mock API 응답과 사이드바 문구 비교 | PASS |
| UI-REVIEW-002 | 대화 시작 경쟁 조건 | `startConversation()`에 연결 중 재진입/pending guard를 두고 WebSocket 참조·마이크·버튼 상태가 한 세션에만 귀속되도록 한다. 빠른 두 번 클릭이 두 WS를 만들지 않아야 한다. | source review: `conversationControlsLocked`, `startingConversation`, socket-local handlers; browser double-click runtime unavailable | PASS (정적) |
| UI-REVIEW-003 | 대화 중 페르소나/고급 입력 잠금 | select/create 버튼뿐 아니라 이름·정체성·관계·고급 textarea/number/details와 생성 패널의 모든 조작을 잠근다. close 시 원상 복구한다. | source review: all input IDs disabled plus `bindPersonaDetailsGuards`; browser click runtime unavailable | PASS (정적) |
| UI-REVIEW-004 | 연결 종료 stale 대화 상태 | `onclose`에서 partial transcript bubble, assistant bubble map/current turn, live transcript와 stage 상태를 현재 세션 기준으로 정리해 다음 세션에 이전 대화의 임시 DOM이 남지 않게 한다. 확정된 대화 기록과 마지막 오류/경고는 보존한다. | source review: `clearTransientConversationUi()` from socket close; browser reconnect runtime unavailable | PASS (정적) |
| UI-REVIEW-005 | 연결 종료 음성/VAD 정리 | `onclose`에서 speechActive/aboveFrames/belowFrames/noise floor 및 pending microphone/VAD 상태를 정리해 다음 세션이 이전 발화로 즉시 종료·시작되지 않게 한다. | source review: `stopMicCapture()` reset and stale packet guard; browser VAD runtime unavailable | PASS (정적) |
| UI-REVIEW-006 | 초기 LIVE 표시 | 초기 `liveLabel`의 텍스트와 offline 스타일이 일치해야 한다. 세션이 시작되기 전 화면은 LIVE 스타일로 보이지 않아야 하며, `session.state`/연결 이벤트 때만 LIVE 스타일로 전환한다. | `index.html` has `class="live-label offline">OFFLINE`; static contract passed; browser first-load unavailable | PASS (정적) |

## 상태 규칙

- `TODO`: 구현·검증 전
- `PASS`: 구현 조건과 실제 동작 증거를 모두 확인
- `FAIL`: 재현 가능한 조건 불충족
- `UNVERIFIED`: 코드/정적 검사는 있으나 실제 런타임 증거 부족
- `BLOCKED`: 외부 서비스·브라우저 권한·하드웨어 등으로 검증을 수행할 수 없음. 차단 원인과 재현 명령을 함께 기록

## 최종 검증 기록 (2026-08-12, Asia/Seoul)

- 정적/문법 증거: `node --check gateway/app/static/app.js` 통과; `python -X utf8 -m pytest -q tests/test_persona_api.py -k 'static or create'` (2 passed); `python -X utf8 -m pytest -q tests/test_integration_contracts.py -k static` (1 passed); `python -X utf8 -m pytest -q tests/test_persona_api.py -k 'not websocket'` (3 passed); `python -X utf8 -m pytest -q tests/test_persona.py` (5 passed); `python -X utf8 -m pytest -q tests/test_voice_enroll_upload.py -k 'config or rejects or static'` (4 passed); `git diff --check` 통과.
- WebSocket/실제 음성 증거: `tests/test_persona_api.py -k websocket` 및 mock WebSocket 계약이 15초 timeout. `gateway/app/providers.py`에 mock provider 구현이 없고 `test_provider_factory.py`가 `MockLLM` import에서 수집 실패해 `session.ready`/audio/마이크 시나리오를 실행할 수 없었다. 해당 항목은 `BLOCKED` 또는 `UNVERIFIED`로 유지했다.
- 브라우저 증거: 이 검증 실행에서 로컬 브라우저/마이크 권한·실제 STT/TTS/LLM 런타임을 확보하지 못해 Network/Console/WS/화면 캡처 증거를 만들 수 없었다. UI-INT-001~004, 006, 008~010은 `UNVERIFIED`, UI-INT-005/007/011은 `BLOCKED`다.
- 범위 밖 변경: README/START_HERE/docs/launcher 삭제·추가 및 `tests/tunnel_fallback_harness.ps1`는 기존 사용자 변경으로 보존했다. launcher 계약 실패는 UI 통합 결과와 분리했다.

최종 통합은 모든 항목이 `PASS`이고 UI-INT-011에 실제 런타임 증거가 있을 때만 완료로 표시한다. 현재는 정적 UI 판정만 PASS이며 최종 통합은 `BLOCKED/UNVERIFIED`다.
