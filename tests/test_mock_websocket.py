from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def load_mock_app():
    os.environ.update(
        {
            "STT_MODE": "mock",
            "TTS_MODE": "mock",
            "LLM_MODE": "mock",
            "PERSONA_DIR": str(ROOT / "gateway" / "personas"),
            "MOCK_TRANSCRIPT": "안녕. 지금 실시간 대화를 시험하고 있어.",
            "BACKCHANNEL_ENABLED": "false",
        }
    )
    for name in list(sys.modules):
        if name.startswith("gateway.app"):
            del sys.modules[name]
    return importlib.import_module("gateway.app.main").app


def receive_event(ws):
    message = ws.receive()
    if message.get("bytes") is not None:
        return {"type": "binary", "payload": message["bytes"]}
    if message.get("text") is not None:
        return json.loads(message["text"])
    return message


def test_mock_full_turn_and_audio_stream() -> None:
    app = load_mock_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/conversation") as ws:
            initial_types: set[str] = set()
            while not {"session.ready", "session.state"} <= initial_types:
                initial_types.add(receive_event(ws)["type"])

            ws.send_json({"type": "session.configure", "persona_id": "default", "voice_profile_id": "mock-voice"})
            while receive_event(ws)["type"] != "session.configured":
                pass

            ws.send_json({"type": "client.speech_start"})
            # One second of non-zero PCM16. The mock STT only counts bytes.
            ws.send_bytes((b"\x10\x00") * 16000)
            ws.send_json({"type": "client.speech_end"})

            saw_final = False
            saw_binary = False
            saw_generation_done = False
            stream_ids: list[int] = []
            turn_id = ""
            for _ in range(300):
                event = receive_event(ws)
                event_type = event.get("type")
                if event_type == "transcript.final":
                    saw_final = True
                elif event_type == "assistant.turn_start":
                    turn_id = event["turn_id"]
                elif event_type == "audio.begin" and event.get("channel") == 1:
                    stream_ids.append(int(event["stream_id"]))
                elif event_type == "binary":
                    saw_binary = True
                    assert len(event["payload"]) > 9
                elif event_type == "assistant.generation_done":
                    saw_generation_done = True
                    break
            assert saw_final
            assert saw_binary
            assert saw_generation_done
            assert turn_id
            assert stream_ids

            for stream_id in stream_ids:
                ws.send_json({"type": "audio.played", "stream_id": stream_id})
            ws.send_json({"type": "assistant.playback_idle", "turn_id": turn_id})

            committed = False
            for _ in range(20):
                event = receive_event(ws)
                if event.get("type") == "assistant.committed":
                    committed = True
                    assert event["spoken_text"]
                    break
            assert committed
