from __future__ import annotations

import importlib
import json
import os
import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def load_gateway(*, tts_mode: str = "mock", persona_data_dir: Path | None = None):
    """Load a fresh gateway module so mock profile state is isolated per test."""

    values = {
        "STT_MODE": "mock",
        "TTS_MODE": tts_mode,
        "LLM_MODE": "mock",
        "PERSONA_DIR": str(ROOT / "gateway" / "personas"),
        "BACKCHANNEL_ENABLED": "false",
    }
    if persona_data_dir is not None:
        values["PERSONA_DATA_DIR"] = str(persona_data_dir)
    os.environ.update(values)
    for name in list(sys.modules):
        if name.startswith("gateway.app"):
            del sys.modules[name]
    return importlib.import_module("gateway.app.main")


def _voice_form() -> dict[str, str]:
    return {
        "transcript": "정확히 읽은 기준 대본입니다.",
        "display_name": "통합 테스트 목소리",
        "consent": "true",
    }


def test_mock_voice_lifecycle_upload_enroll_warmup_and_list() -> None:
    main = load_gateway()
    with TestClient(main.app) as client:
        enrolled = client.post(
            "/api/voices/enroll",
            data=_voice_form(),
            files={"audio": ("reference.wav", b"mock-audio", "audio/wav")},
        )
        assert enrolled.status_code == 200
        profile = enrolled.json()
        profile_id = profile["profile_id"]
        assert profile["display_name"] == "통합 테스트 목소리"
        assert profile["consent_recorded"] is True

        warmed = client.post(f"/api/voices/{profile_id}/warmup")
        assert warmed.status_code == 200
        assert warmed.json() == {"ok": True, "profile_id": profile_id, "seconds": 0.0}

        listed = client.get("/api/voices")
        assert listed.status_code == 200
        assert any(
            item["profile_id"] == profile_id and item["display_name"] == "통합 테스트 목소리"
            for item in listed.json()
        )


def test_voice_update_and_delete_contract_in_mock_mode() -> None:
    main = load_gateway()
    with TestClient(main.app) as client:
        enrolled = client.post(
            "/api/voices/enroll",
            data=_voice_form(),
            files={"audio": ("reference.wav", b"mock-audio", "audio/wav")},
        )
        profile_id = enrolled.json()["profile_id"]

        updated = client.patch(
            f"/api/voices/{profile_id}",
            json={"display_name": "수정된 목소리"},
        )
        assert updated.status_code == 200
        assert updated.json()["profile_id"] == profile_id
        assert updated.json()["display_name"] == "수정된 목소리"

        listed = client.get("/api/voices").json()
        assert next(item for item in listed if item["profile_id"] == profile_id)["display_name"] == "수정된 목소리"

        deleted = client.delete(f"/api/voices/{profile_id}")
        assert deleted.status_code == 200
        assert deleted.json() == {"ok": True, "profile_id": profile_id}
        assert profile_id not in main._mock_voice_profiles
        assert all(item["profile_id"] != profile_id for item in client.get("/api/voices").json())
        assert client.delete(f"/api/voices/{profile_id}").status_code == 404


def test_voice_update_and_delete_forward_to_qwen_service(monkeypatch) -> None:
    main = load_gateway(tts_mode="qwen_ws")
    calls: list[tuple[str, str, dict[str, object]]] = []

    async def fake_service_json(method: str, url: str, **kwargs):
        calls.append((method, url, kwargs))
        if method == "PATCH":
            return {"profile_id": "voice-test", "display_name": "수정된 목소리"}
        return {"ok": True, "profile_id": "voice-test"}

    monkeypatch.setattr(main, "_service_json", fake_service_json)
    with TestClient(main.app) as client:
        updated = client.patch("/api/voices/voice-test", json={"display_name": "수정된 목소리"})
        deleted = client.delete("/api/voices/voice-test")

    assert updated.status_code == 200
    assert deleted.status_code == 200
    assert calls == [
        (
            "PATCH",
            f"{main.settings.tts_http_url}/profiles/voice-test",
            {"json": {"display_name": "수정된 목소리"}},
        ),
        ("DELETE", f"{main.settings.tts_http_url}/profiles/voice-test", {}),
    ]


def _receive_json(websocket) -> dict:
    message = websocket.receive()
    if message.get("text") is None:
        return {}
    return json.loads(message["text"])


def test_simplified_persona_create_list_and_select_contract(tmp_path: Path) -> None:
    main = load_gateway(persona_data_dir=tmp_path)
    payload = {
        "name": "간결한 검토자",
        "identity": "차분하고 정확한 검토 도우미",
        "relationship": "사용자의 설계 검토 파트너",
    }
    with TestClient(main.app) as client:
        created_response = client.post("/api/personas", json=payload)
        assert created_response.status_code == 201
        created = created_response.json()["persona"]
        assert created["name"] == payload["name"]
        assert created["identity"] == payload["identity"]
        assert created["source"] == "custom"

        listed = client.get("/api/personas")
        assert listed.status_code == 200
        assert any(item["id"] == created["id"] and item["source"] == "custom" for item in listed.json())

        with client.websocket_connect("/ws/conversation") as websocket:
            ready = set()
            while not {"session.ready", "session.state"} <= ready:
                ready.add(_receive_json(websocket).get("type"))
            websocket.send_json(
                {
                    "type": "session.configure",
                    "persona_id": created["id"],
                    "voice_profile_id": "mock-voice",
                }
            )
            configured = None
            for _ in range(5):
                event = _receive_json(websocket)
                if event.get("type") == "session.configured":
                    configured = event
                    break
            assert configured == {
                "type": "session.configured",
                "persona": created["id"],
                "persona_name": payload["name"],
                "voice_profile_id": "mock-voice",
            }


def test_simplified_persona_static_select_contract() -> None:
    html = (ROOT / "gateway" / "app" / "static" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "gateway" / "app" / "static" / "app.js").read_text(encoding="utf-8")

    for field in ("personaName", "personaIdentity", "personaRelationship", "personaSelect", "createPersona", "selectCreatedPersona"):
        assert f'id="{field}"' in html
    assert 'api("/api/personas"' in script
    assert "state.createdPersonaId" in script
    assert "selectPersona(state.createdPersonaId)" in script
    assert "대화를 시작하려면 대화 시작을 누르세요" in script
    assert "startConversation()" not in script[script.index("async function submitPersona"): script.index("async function refreshVoices")]


def test_voice_sources_are_separate_and_submit_actual_selected_file() -> None:
    html = (ROOT / "gateway" / "app" / "static" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "gateway" / "app" / "static" / "app.js").read_text(encoding="utf-8")

    for element_id in (
        "recordReference",
        "stopReference",
        "recordingPreview",
        "enrollRecordedVoice",
        "referenceUpload",
        "uploadPreview",
        "enrollUploadedVoice",
    ):
        assert f'id="{element_id}"' in html
    assert 'enrollVoice("recording")' in script
    assert 'enrollVoice("upload")' in script
    assert 'form.append("audio", file' in script
    assert "referenceBlob" not in script
    assert 'id="enrollVoice"' not in html


def test_source_backed_launchers_always_rebuild_images() -> None:
    powershell = (ROOT / "scripts" / "persona-duplex.ps1").read_text(encoding="utf-8")
    invoke_up_start = powershell.index("function Invoke-Up")
    invoke_up_end = powershell.index("function Start-Local", invoke_up_start)
    invoke_up = powershell[invoke_up_start:invoke_up_end]
    assert '$composeArgs += @("up")' in invoke_up
    assert '$buildRequired = $true' in invoke_up
    assert '$composeArgs += "--build"' in invoke_up
    assert 'if ($LASTEXITCODE -ne 0) { $buildRequired = $true }' not in invoke_up
    for service in ("gateway", "qwen-asr", "qwen-tts"):
        assert f'"{service}"' in invoke_up

    bash = (ROOT / "scripts" / "persona-duplex.sh").read_text(encoding="utf-8")
    assert re.search(r"compose .* up --build -d gateway", bash)
    assert re.search(r"compose .* up --build -d gateway qwen-asr qwen-tts", bash)
    assert re.search(r"compose .* up --build -d gateway qwen-tts", bash)


def test_compose_keeps_source_backed_services_buildable() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    for dockerfile in ("gateway/Dockerfile", "services/qwen_asr/Dockerfile", "services/qwen_tts/Dockerfile"):
        assert f"dockerfile: {dockerfile}" in compose
    assert "./data/personas:/data/personas" in compose
    assert "./data/voices:/data/voices" in compose
