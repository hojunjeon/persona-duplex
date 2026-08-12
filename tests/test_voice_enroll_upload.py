from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def load_main(tts_mode: str = "mock"):
    os.environ.update(
        {
            "STT_MODE": "mock",
            "TTS_MODE": tts_mode,
            "LLM_MODE": "mock",
            "PERSONA_DIR": str(ROOT / "gateway" / "personas"),
        }
    )
    for name in list(sys.modules):
        if name.startswith("gateway.app"):
            del sys.modules[name]
    return importlib.import_module("gateway.app.main")


def valid_form() -> dict[str, str]:
    return {
        "transcript": "정확히 읽은 기준 대본입니다.",
        "display_name": "테스트 목소리",
        "consent": "true",
    }


def test_config_exposes_actual_voice_upload_limit(monkeypatch) -> None:
    monkeypatch.delenv("VOICE_MAX_UPLOAD_BYTES", raising=False)
    main = load_main()
    with TestClient(main.app) as client:
        response = client.get("/api/config")

    assert response.status_code == 200
    assert main.VOICE_MAX_UPLOAD_BYTES == 32 * 1024 * 1024
    assert response.json()["voice_max_upload_bytes"] == main.VOICE_MAX_UPLOAD_BYTES


def test_mock_enroll_accepts_supported_upload() -> None:
    main = load_main()
    with TestClient(main.app) as client:
        response = client.post(
            "/api/voices/enroll",
            data=valid_form(),
            files={"audio": ("take.wav", b"wav-payload", "audio/wav")},
        )

    assert response.status_code == 200
    assert response.json()["display_name"] == "테스트 목소리"


def test_enroll_rejects_missing_consent_and_unsupported_suffix() -> None:
    main = load_main()
    with TestClient(main.app) as client:
        no_consent = client.post(
            "/api/voices/enroll",
            data={**valid_form(), "consent": "false"},
            files={"audio": ("take.wav", b"wav-payload", "audio/wav")},
        )
        unsupported = client.post(
            "/api/voices/enroll",
            data=valid_form(),
            files={"audio": ("take.txt", b"audio-payload", "audio/wav")},
        )

    assert no_consent.status_code == 400
    assert "동의" in no_consent.json()["detail"]
    assert unsupported.status_code == 400
    assert "지원하지 않는" in unsupported.json()["detail"]


def test_enroll_rejects_empty_payload() -> None:
    main = load_main()
    with TestClient(main.app) as client:
        response = client.post(
            "/api/voices/enroll",
            data=valid_form(),
            files={"audio": ("empty.wav", b"", "audio/wav")},
        )

    assert response.status_code == 400
    assert "빈" in response.json()["detail"]


def test_filename_is_basename_only_and_empty_filename_has_safe_fallback() -> None:
    main = load_main()
    assert main._safe_upload_filename(r"..\nested\take.wav") == "take.wav"
    assert main._safe_upload_filename("") == "reference.webm"


def test_enroll_rejects_oversize_before_tts_forwarding() -> None:
    main = load_main()
    main.VOICE_MAX_UPLOAD_BYTES = 4
    with TestClient(main.app) as client:
        response = client.post(
            "/api/voices/enroll",
            data=valid_form(),
            files={"audio": ("take.wav", b"12345", "audio/wav")},
        )

    assert response.status_code == 413
    assert "4바이트" in response.json()["detail"]


def test_qwen_forwarding_preserves_safe_filename_mime_payload_and_form(monkeypatch) -> None:
    main = load_main("qwen_ws")
    captured: dict[str, object] = {}

    async def fake_service_json(method: str, url: str, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"profile_id": "voice-test", "display_name": "테스트 목소리"}

    monkeypatch.setattr(main, "_service_json", fake_service_json)
    with TestClient(main.app) as client:
        response = client.post(
            "/api/voices/enroll",
            data=valid_form(),
            files={"audio": (r"nested\\take.M4A", b"payload", "audio/custom")},
        )

    assert response.status_code == 200
    assert captured["method"] == "POST"
    files = captured["files"]
    assert files["audio"] == ("take.M4A", b"payload", "audio/custom")
    assert captured["data"] == {
        "transcript": "정확히 읽은 기준 대본입니다.",
        "display_name": "테스트 목소리",
        "consent": "true",
    }
