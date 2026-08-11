from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .persona import list_personas
from .providers import create_llm, create_stt, create_tts
from .session import VoiceSession

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

app = FastAPI(title=settings.app_name, version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.client_origin] if settings.client_origin != "*" else ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _effective_stt_api_key() -> str:
    if settings.stt_api_key:
        return settings.stt_api_key
    return {
        "elevenlabs_ws": settings.elevenlabs_api_key,
        "soniox_ws": settings.soniox_api_key,
        "deepgram_ws": settings.deepgram_api_key,
    }.get(settings.stt_mode, "")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/benchmark")
def benchmark_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "benchmark.html")


@app.get("/api/config")
def public_config() -> dict[str, Any]:
    return {
        "app_name": settings.app_name,
        "stt_mode": settings.stt_mode,
        "stt_model": settings.stt_cloud_model if settings.stt_mode in {"elevenlabs_ws", "soniox_ws", "deepgram_ws"} else "Qwen3-ASR",
        "tts_mode": settings.tts_mode,
        "llm_mode": settings.llm_mode,
        "llm_model": settings.llm_model,
        "default_persona": settings.default_persona,
        "personas": list_personas(settings.persona_dir),
        "audio": {
            "input_sample_rate": 16000,
            "barge_in_confirm_ms": settings.barge_in_confirm_ms,
            "backchannel_enabled": settings.backchannel_enabled,
            "pre_roll_ms": settings.pre_roll_ms,
            "vad_start_frames": settings.vad_start_frames,
            "vad_end_ms_assistant": settings.vad_end_ms_assistant,
            "vad_end_ms_idle": settings.vad_end_ms_idle,
            "vad_threshold_min": settings.vad_threshold_min,
            "vad_noise_multiplier_assistant": settings.vad_noise_multiplier_assistant,
            "vad_noise_multiplier_idle": settings.vad_noise_multiplier_idle,
        },
    }


async def _service_json(method: str, url: str, **kwargs: Any) -> Any:
    timeout = httpx.Timeout(180, connect=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(method, url, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except Exception:
                detail = response.text[-800:]
            raise HTTPException(status_code=response.status_code, detail=detail or "speech service error")
        if not response.content:
            return {"ok": True}
        return response.json()


@app.get("/api/health")
async def health() -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True, "gateway": "ok"}
    if settings.stt_mode == "qwen_ws":
        try:
            result["stt"] = await _service_json("GET", f"{settings.stt_http_url}/health")
        except Exception as exc:
            result["ok"] = False
            result["stt"] = {"ok": False, "error": str(exc)}
    elif settings.stt_mode in {"elevenlabs_ws", "soniox_ws", "deepgram_ws"}:
        api_key = _effective_stt_api_key()
        result["stt"] = {"ok": bool(api_key), "mode": settings.stt_mode, "model": settings.stt_cloud_model}
        if not api_key:
            result["ok"] = False
    else:
        result["stt"] = {"ok": True, "mode": "mock"}

    if settings.tts_mode == "qwen_ws":
        try:
            result["tts"] = await _service_json("GET", f"{settings.tts_http_url}/health")
        except Exception as exc:
            result["ok"] = False
            result["tts"] = {"ok": False, "error": str(exc)}
    else:
        result["tts"] = {"ok": True, "mode": "mock"}
    return result




def _benchmark_manifest_rows() -> list[dict[str, str]]:
    manifest = settings.benchmark_dir / "manifest.csv"
    if not manifest.exists():
        return []
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


@app.get("/api/benchmark/samples")
def benchmark_samples() -> list[dict[str, str]]:
    return _benchmark_manifest_rows()


@app.post("/api/benchmark/samples")
async def save_benchmark_sample(
    audio: UploadFile = File(...),
    sample_id: str = Form(...),
    reference: str = Form(...),
) -> dict[str, Any]:
    sample_id = sample_id.strip()
    reference = re.sub(r"\s+", " ", reference).strip()
    if not re.fullmatch(r"[0-9]{2,3}", sample_id):
        raise HTTPException(status_code=400, detail="sample_id는 두세 자리 숫자여야 합니다.")
    if len(reference) < 2 or len(reference) > 500:
        raise HTTPException(status_code=400, detail="기준 문장은 2~500자여야 합니다.")
    payload = await audio.read()
    if not payload or len(payload) > 24 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="벤치마크 녹음 파일 크기가 올바르지 않습니다.")

    suffix = Path(audio.filename or "sample.webm").suffix.lower()
    if suffix not in {".webm", ".wav", ".mp4", ".m4a", ".ogg", ".opus"}:
        suffix = ".webm"
    samples_dir = settings.benchmark_dir / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    existing_rows = _benchmark_manifest_rows()
    filename = f"{sample_id}{suffix}"
    for old in existing_rows:
        if old.get("sample_id") == sample_id:
            old_path = settings.benchmark_dir / str(old.get("audio_path") or "")
            if old_path.is_file() and old_path.name != filename:
                old_path.unlink(missing_ok=True)
    (samples_dir / filename).write_bytes(payload)

    rows = [row for row in existing_rows if row.get("sample_id") != sample_id]
    rows.append({"sample_id": sample_id, "audio_path": f"samples/{filename}", "reference": reference})
    rows.sort(key=lambda row: row["sample_id"])
    manifest = settings.benchmark_dir / "manifest.csv"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sample_id", "audio_path", "reference"])
        writer.writeheader()
        writer.writerows(rows)
    return {"ok": True, "sample_id": sample_id, "audio_path": f"samples/{filename}", "count": len(rows)}


@app.get("/api/voices")
async def list_voices() -> Any:
    if settings.tts_mode == "mock":
        return [{"profile_id": "mock-voice", "display_name": "Mock voice", "seconds": 8.0}]
    return await _service_json("GET", f"{settings.tts_http_url}/profiles")


@app.post("/api/voices/enroll")
async def enroll_voice(
    audio: UploadFile = File(...),
    transcript: str = Form(...),
    display_name: str = Form("내 목소리"),
    consent: bool = Form(...),
) -> Any:
    if settings.tts_mode == "mock":
        if not consent:
            raise HTTPException(status_code=400, detail="동의가 필요합니다.")
        return {
            "profile_id": "mock-voice",
            "display_name": display_name,
            "transcript": transcript,
            "seconds": 8.0,
            "consent_recorded": True,
        }
    payload = await audio.read()
    files = {"audio": (audio.filename or "reference.webm", payload, audio.content_type or "application/octet-stream")}
    data = {"transcript": transcript, "display_name": display_name, "consent": str(consent).lower()}
    return await _service_json("POST", f"{settings.tts_http_url}/profiles", files=files, data=data)


@app.post("/api/voices/{profile_id}/warmup")
async def warm_voice(profile_id: str) -> Any:
    if settings.tts_mode == "mock":
        return {"ok": True, "profile_id": profile_id, "seconds": 0.0}
    return await _service_json("POST", f"{settings.tts_http_url}/profiles/{profile_id}/warmup")


@app.delete("/api/voices/{profile_id}")
async def delete_voice(profile_id: str) -> Any:
    if settings.tts_mode == "mock":
        return {"ok": True, "profile_id": profile_id}
    return await _service_json("DELETE", f"{settings.tts_http_url}/profiles/{profile_id}")


@app.websocket("/ws/conversation")
async def conversation(websocket: WebSocket) -> None:
    await websocket.accept()
    session: VoiceSession | None = None
    try:
        stt = create_stt(
            settings.stt_mode,
            url=settings.stt_ws_url,
            language=settings.stt_language,
            mock_transcript=settings.mock_transcript,
            api_key=_effective_stt_api_key(),
            cloud_model=settings.stt_cloud_model,
            cloud_language_code=settings.stt_cloud_language_code,
        )
        llm = create_llm(
            settings.llm_mode,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
        )
        tts = create_tts(settings.tts_mode, url=settings.tts_ws_url)
        session = VoiceSession(websocket, settings=settings, stt=stt, llm=llm, tts=tts)
        await session.start()
        while True:
            message = await websocket.receive()
            if message.get("bytes") is not None:
                await session.handle_audio(message["bytes"])
            elif message.get("text") is not None:
                payload = json.loads(message["text"])
                await session.handle_control(payload)
            elif message.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "source": "gateway", "message": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
    finally:
        if session is not None:
            await session.close()
        try:
            await websocket.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, workers=1)
