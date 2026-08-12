from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def load_app(data_dir: Path):
    os.environ.update(
        {
            "STT_MODE": "mock",
            "TTS_MODE": "mock",
            "LLM_MODE": "mock",
            "PERSONA_DIR": str(ROOT / "gateway" / "personas"),
            "PERSONA_DATA_DIR": str(data_dir),
            "BACKCHANNEL_ENABLED": "false",
        }
    )
    for name in list(sys.modules):
        if name.startswith("gateway.app"):
            del sys.modules[name]
    return importlib.import_module("gateway.app.main").app


def persona_payload(**overrides):
    payload = {
        "name": "작업 도우미",
        "identity": "차분한 협력형 AI",
        "relationship": "사용자의 검토 파트너",
        "speaking_style": ["결론부터 말한다."],
        "behavior": ["확인된 사실과 추정을 나눈다."],
        "boundaries": ["모르는 내용은 모른다고 말한다."],
        "backchannels": ["응.", "음."],
        "max_sentences": 2,
    }
    payload.update(overrides)
    return payload


def test_persona_get_post_validation_duplicate_and_persistence(tmp_path: Path) -> None:
    app = load_app(tmp_path)
    with TestClient(app) as client:
        listed = client.get("/api/personas")
        assert listed.status_code == 200
        assert any(item["id"] == "default" and item["source"] == "builtin" for item in listed.json())

        response = client.post("/api/personas", json=persona_payload(id="custom-reviewer"))
        assert response.status_code == 201
        created = response.json()
        assert created["ok"] is True
        assert created["persona"] == {
            "id": "custom-reviewer",
            "name": "작업 도우미",
            "identity": "차분한 협력형 AI",
            "source": "custom",
        }
        assert any(item["id"] == "custom-reviewer" and item["source"] == "custom" for item in created["personas"])
        assert (tmp_path / "custom-reviewer.yaml").is_file()

        duplicate = client.post("/api/personas", json=persona_payload(id="custom-reviewer"))
        assert duplicate.status_code == 409
        builtin = client.post("/api/personas", json=persona_payload(id="default"))
        assert builtin.status_code == 409

        assert client.post("/api/personas", json=persona_payload(name="\u0000bad")).status_code == 422
        assert client.post("/api/personas", json=persona_payload(speaking_style="not-array")).status_code == 422
        assert client.post("/api/personas", json=persona_payload(max_sentences=9)).status_code == 422
        assert client.post("/api/personas", json=persona_payload(id="../escape")).status_code == 422

        config = client.get("/api/config").json()
        assert any(item["id"] == "custom-reviewer" for item in config["personas"])


def _receive_json(ws):
    message = ws.receive()
    if message.get("text") is None:
        return {}
    return json.loads(message["text"])


def test_websocket_can_configure_custom_persona(tmp_path: Path) -> None:
    app = load_app(tmp_path)
    with TestClient(app) as client:
        created = client.post("/api/personas", json=persona_payload(id="custom-voice-test")).json()["persona"]
        with client.websocket_connect("/ws/conversation") as ws:
            ready = set()
            while not {"session.ready", "session.state"} <= ready:
                ready.add(_receive_json(ws).get("type"))
            ws.send_json({"type": "session.configure", "persona_id": created["id"], "voice_profile_id": "mock-voice"})
            configured = None
            for _ in range(5):
                event = _receive_json(ws)
                if event.get("type") == "session.configured":
                    configured = event
                    break
            assert configured is not None
            assert configured["persona"] == "custom-voice-test"
            assert configured["persona_name"] == "작업 도우미"
            ws.send_json({"type": "session.configure", "persona_id": "default", "voice_profile_id": "mock-voice"})
            rejected = None
            for _ in range(5):
                event = _receive_json(ws)
                if event.get("type") == "error" and event.get("source") == "persona":
                    rejected = event
                    break
            assert rejected is not None
            assert "바꿀 수 없습니다" in rejected["message"]


def test_static_persona_create_contract() -> None:
    html = (ROOT / "gateway" / "app" / "static" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "gateway" / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="personaSelect"' in html
    assert 'id="personaCreatePanel"' in html
    for field in ("personaName", "personaIdentity", "personaRelationship", "personaSpeakingStyle", "personaBehavior", "personaBoundaries", "personaBackchannels", "personaMaxSentences", "createPersona"):
        assert f'id="{field}"' in html
    assert 'api("/api/personas"' in script
    assert 'type: "session.configure"' in script
    assert "activePersonaId" in script
    assert "voice_profile_id" in script
    assert "const previousPersonaId = state.selectedPersona" in script
    assert "refreshPersonas(previousPersonaId)" in script
    assert "기존 선택을 유지했습니다" in script
    assert "refreshPersonas(response.persona?.id || \"\")" not in script
