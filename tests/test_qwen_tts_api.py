from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("persona_qwen_tts_server", ROOT / "services" / "qwen_tts" / "server.py")
assert SPEC and SPEC.loader
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class FakeFasterModel:
    def __init__(self) -> None:
        self.kwargs = None

    def generate_voice_clone_streaming(self, **kwargs):
        self.kwargs = kwargs
        yield np.array([0.0, 0.25, -0.25], dtype=np.float32), 24000, {"ttfa_ms": 123}


class FakeOfficialModel:
    def __init__(self) -> None:
        self.prompt_kwargs = None
        self.generate_kwargs = None

    def create_voice_clone_prompt(self, **kwargs):
        self.prompt_kwargs = kwargs
        return ["prompt"]

    def generate_voice_clone(self, **kwargs):
        self.generate_kwargs = kwargs
        return [np.array([0.0, 0.1], dtype=np.float32)], 24000


def profile() -> dict:
    return {
        "audio_path": "/tmp/reference.wav",
        "transcript": "참조 음성의 정확한 대본입니다.",
        "sha256": "a" * 64,
    }


def test_faster_backend_uses_public_reference_audio_api(monkeypatch) -> None:
    model = FakeFasterModel()
    monkeypatch.setattr(server, "_load_model", lambda: (model, "faster-qwen3-tts"))
    chunks = list(server._iter_audio(profile(), "새 문장", "Korean", 4))

    assert chunks and chunks[0][1] == 24000
    assert model.kwargs == {
        "text": "새 문장",
        "language": "Korean",
        "ref_audio": "/tmp/reference.wav",
        "ref_text": "참조 음성의 정확한 대본입니다.",
        "chunk_size": 4,
    }


def test_official_backend_reuses_full_icl_prompt(monkeypatch) -> None:
    model = FakeOfficialModel()
    monkeypatch.setattr(server, "_load_model", lambda: (model, "official-qwen-tts"))
    server._prompt_cache.clear()

    chunks = list(server._iter_audio(profile(), "새 문장", "Korean", 4))

    assert chunks
    assert model.prompt_kwargs == {
        "ref_audio": "/tmp/reference.wav",
        "ref_text": "참조 음성의 정확한 대본입니다.",
        "x_vector_only_mode": False,
    }
    assert model.generate_kwargs == {
        "text": "새 문장",
        "language": "Korean",
        "voice_clone_prompt": ["prompt"],
    }
