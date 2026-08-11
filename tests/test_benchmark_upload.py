from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def load_app(benchmark_dir: Path):
    os.environ.update(
        {
            "STT_MODE": "mock",
            "TTS_MODE": "mock",
            "LLM_MODE": "mock",
            "PERSONA_DIR": str(ROOT / "gateway" / "personas"),
            "BENCHMARK_DIR": str(benchmark_dir),
        }
    )
    for name in list(sys.modules):
        if name.startswith("gateway.app"):
            del sys.modules[name]
    return importlib.import_module("gateway.app.main").app


def test_benchmark_sample_upload_and_manifest(tmp_path: Path) -> None:
    app = load_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/benchmark/samples",
            data={"sample_id": "01", "reference": "안녕하세요. 테스트 문장입니다."},
            files={"audio": ("01.webm", b"fake-webm-payload", "audio/webm")},
        )
        assert response.status_code == 200
        assert response.json()["count"] == 1
        rows = client.get("/api/benchmark/samples").json()
        assert rows == [
            {
                "sample_id": "01",
                "audio_path": "samples/01.webm",
                "reference": "안녕하세요. 테스트 문장입니다.",
            }
        ]
        assert (tmp_path / "samples" / "01.webm").read_bytes() == b"fake-webm-payload"
        assert client.get("/benchmark").status_code == 200
