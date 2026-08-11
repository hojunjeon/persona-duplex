from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from collections import OrderedDict
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterator

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

MODEL_ID = os.getenv("TTS_MODEL_ID", "Qwen/Qwen3-TTS-12Hz-0.6B-Base")
BACKEND = os.getenv("TTS_BACKEND", "auto").lower()
PORT = int(os.getenv("TTS_PORT", "8102"))
PROFILE_DIR = Path(os.getenv("VOICE_PROFILE_DIR", "/data/voices"))
MIN_SECONDS = float(os.getenv("VOICE_MIN_SECONDS", "3"))
MAX_SECONDS = float(os.getenv("VOICE_MAX_SECONDS", "45"))
MAX_UPLOAD_BYTES = int(os.getenv("VOICE_MAX_UPLOAD_BYTES", str(32 * 1024 * 1024)))
PROMPT_CACHE_SIZE = int(os.getenv("TTS_PROMPT_CACHE_SIZE", "8"))
DEFAULT_CHUNK_SIZE = int(os.getenv("TTS_CHUNK_SIZE", "4"))

PROFILE_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="Persona Duplex Qwen TTS", version="1.0")
_model: Any = None
_model_backend = "unloaded"
_model_error: str | None = None
_model_lock = threading.RLock()
_prompt_cache: OrderedDict[str, Any] = OrderedDict()


def _device_name() -> str:
    try:
        import torch

        return torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    except Exception:
        return "unknown"


def _load_model() -> tuple[Any, str]:
    global _model, _model_backend, _model_error
    if _model is not None:
        return _model, _model_backend
    with _model_lock:
        if _model is not None:
            return _model, _model_backend
        errors: list[str] = []
        if BACKEND in {"auto", "faster"}:
            try:
                from faster_qwen3_tts import FasterQwen3TTS

                model = FasterQwen3TTS.from_pretrained(MODEL_ID)
                if hasattr(model, "warmup"):
                    model.warmup(prefill_len=100)
                _model = model
                _model_backend = "faster-qwen3-tts"
                _model_error = None
                return _model, _model_backend
            except Exception as exc:
                errors.append(f"faster-qwen3-tts: {type(exc).__name__}: {exc}")
                if BACKEND == "faster":
                    _model_error = errors[-1]
                    raise

        if BACKEND in {"auto", "official"}:
            try:
                import torch
                from qwen_tts import Qwen3TTSModel

                if torch.cuda.is_available():
                    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
                    device_map = "cuda:0"
                    attention = "sdpa"
                else:
                    dtype = torch.float32
                    device_map = "cpu"
                    attention = "eager"
                _model = Qwen3TTSModel.from_pretrained(
                    MODEL_ID,
                    device_map=device_map,
                    dtype=dtype,
                    attn_implementation=attention,
                )
                _model_backend = "official-qwen-tts"
                _model_error = None
                return _model, _model_backend
            except Exception as exc:
                errors.append(f"official-qwen-tts: {type(exc).__name__}: {exc}")

        _model_error = " | ".join(errors) or f"unsupported TTS_BACKEND={BACKEND}"
        raise RuntimeError(_model_error)


def _profile_path(profile_id: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{4,80}", profile_id):
        raise HTTPException(status_code=400, detail="잘못된 목소리 프로필 ID입니다.")
    path = PROFILE_DIR / profile_id
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="목소리 프로필을 찾을 수 없습니다.")
    return path


def _read_profile(profile_id: str) -> dict[str, Any]:
    path = _profile_path(profile_id)
    metadata_path = path / "metadata.json"
    if not metadata_path.exists():
        raise HTTPException(status_code=500, detail="목소리 프로필 메타데이터가 손상되었습니다.")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["audio_path"] = str(path / "reference.wav")
    return metadata


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _audio_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate())


async def _store_upload(upload: UploadFile, target_dir: Path) -> Path:
    source = target_dir / ("source" + (Path(upload.filename or "audio.bin").suffix[:10] or ".bin"))
    size = 0
    with source.open("wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="녹음 파일이 너무 큽니다.")
            out.write(chunk)
    if size == 0:
        raise HTTPException(status_code=400, detail="빈 녹음 파일입니다.")
    return source


def _normalize_audio(source: Path, target: Path) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "24000",
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        raise HTTPException(status_code=400, detail=f"오디오 변환 실패: {result.stderr[-600:]}")




def _audio_stats(path: Path) -> dict[str, Any]:
    """Return conservative signal-quality diagnostics without changing the voice."""
    with wave.open(str(path), "rb") as wav:
        sample_rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if width != 2 or channels != 1:
        raise HTTPException(status_code=500, detail="정규화된 참조 음성 형식이 올바르지 않습니다.")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if samples.size == 0:
        raise HTTPException(status_code=400, detail="참조 음성에 오디오 샘플이 없습니다.")
    abs_samples = np.abs(samples)
    peak = float(abs_samples.max())
    rms = float(np.sqrt(np.mean(np.square(samples))))
    rms_dbfs = 20.0 * float(np.log10(max(rms, 1e-9)))
    clipping_ratio = float(np.mean(abs_samples >= 0.995))
    dc_offset = float(np.mean(samples))

    frame_size = max(1, int(sample_rate * 0.02))
    active = 0
    total = 0
    threshold = 10 ** (-45.0 / 20.0)
    for start in range(0, samples.size, frame_size):
        block = samples[start : start + frame_size]
        if block.size < frame_size // 2:
            continue
        total += 1
        block_rms = float(np.sqrt(np.mean(np.square(block))))
        if block_rms >= threshold:
            active += 1
    active_ratio = active / max(total, 1)

    warnings: list[str] = []
    if rms_dbfs < -36:
        warnings.append("녹음 레벨이 낮습니다. 마이크를 조금 더 가깝게 두세요.")
    elif rms_dbfs > -8:
        warnings.append("녹음 레벨이 큽니다. 입력 게인을 낮추세요.")
    if clipping_ratio > 0.001:
        warnings.append("클리핑이 감지되었습니다. 입력 게인을 낮춰 다시 녹음하는 편이 좋습니다.")
    if active_ratio < 0.35:
        warnings.append("무음 비율이 높습니다. 발화 앞뒤의 긴 침묵을 줄이세요.")
    if abs(dc_offset) > 0.02:
        warnings.append("DC 오프셋이 큽니다. 다른 입력 장치나 녹음 앱을 확인하세요.")

    quality = "good"
    if warnings:
        quality = "warning"
    if clipping_ratio > 0.02 or rms_dbfs < -48 or active_ratio < 0.12:
        quality = "poor"

    return {
        "quality": quality,
        "peak": round(peak, 5),
        "rms_dbfs": round(rms_dbfs, 2),
        "clipping_ratio": round(clipping_ratio, 6),
        "active_ratio": round(active_ratio, 4),
        "dc_offset": round(dc_offset, 6),
        "warnings": warnings,
    }


def _prompt_key(profile: dict[str, Any]) -> str:
    return f"{profile['sha256']}:{hashlib.sha256(profile['transcript'].encode('utf-8')).hexdigest()}"


def _get_prompt(profile: dict[str, Any]) -> Any:
    """Build and cache an official Qwen reusable full-ICL clone prompt.

    ``faster-qwen3-tts`` intentionally exposes reference audio/text directly on
    ``generate_voice_clone_streaming``. Its current public API does not require
    (or promise) the upstream prompt object, so that backend bypasses this helper.
    """
    model, backend = _load_model()
    if backend != "official-qwen-tts":
        raise RuntimeError("reusable prompt objects are only used by the official qwen-tts backend")
    key = _prompt_key(profile)
    with _model_lock:
        if key in _prompt_cache:
            prompt = _prompt_cache.pop(key)
            _prompt_cache[key] = prompt
            return prompt
        prompt = model.create_voice_clone_prompt(
            ref_audio=profile["audio_path"],
            ref_text=profile["transcript"],
            x_vector_only_mode=False,
        )
        _prompt_cache[key] = prompt
        while len(_prompt_cache) > PROMPT_CACHE_SIZE:
            _prompt_cache.popitem(last=False)
        return prompt


def _timing_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items() if isinstance(v, (str, int, float, bool, type(None)))}
    if is_dataclass(value):
        return _timing_dict(asdict(value))
    result: dict[str, Any] = {}
    for name in ("ttfa_ms", "rtf", "elapsed_s", "audio_seconds"):
        if hasattr(value, name):
            item = getattr(value, name)
            if isinstance(item, (int, float)):
                result[name] = item
    return result or {"repr": repr(value)[:200]}


def _to_pcm16(audio: Any) -> bytes:
    values = np.asarray(audio, dtype=np.float32).reshape(-1)
    if values.size == 0:
        return b""
    values = np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)
    peak = float(np.max(np.abs(values)))
    if peak > 1.0:
        values = values / peak * 0.98
    return (np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _iter_audio(
    profile: dict[str, Any],
    text: str,
    language: str,
    chunk_size: int,
) -> Iterator[tuple[bytes, int, dict[str, Any]]]:
    model, backend = _load_model()
    with _model_lock:
        if backend == "faster-qwen3-tts":
            # Public faster-qwen3-tts API: full ICL is the default and consumes
            # ref_audio/ref_text directly. The backend keeps its own reference cache.
            iterator = model.generate_voice_clone_streaming(
                text=text,
                language=language,
                ref_audio=profile["audio_path"],
                ref_text=profile["transcript"],
                chunk_size=chunk_size,
            )
            for audio, sample_rate, timing in iterator:
                pcm = _to_pcm16(audio)
                if pcm:
                    yield pcm, int(sample_rate), _timing_dict(timing)
        else:
            prompt = _get_prompt(profile)
            wavs, sample_rate = model.generate_voice_clone(
                text=text,
                language=language,
                voice_clone_prompt=prompt,
            )
            pcm = _to_pcm16(wavs[0])
            if pcm:
                yield pcm, int(sample_rate), {"fallback": "full-clause"}


def _wav_bytes(chunks: list[bytes], sample_rate: int) -> bytes:
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"".join(chunks))
    return buffer.getvalue()


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": _model_error is None,
        "model": MODEL_ID,
        "configured_backend": BACKEND,
        "loaded_backend": _model_backend,
        "loaded": _model is not None,
        "device": _device_name(),
        "error": _model_error,
    }


@app.post("/warmup")
def warmup() -> dict[str, Any]:
    started = time.perf_counter()
    model, backend = _load_model()
    del model
    return {"ok": True, "backend": backend, "seconds": time.perf_counter() - started}


@app.get("/profiles")
def profiles() -> list[dict[str, Any]]:
    result = []
    for metadata_path in sorted(PROFILE_DIR.glob("*/metadata.json")):
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            result.append(data)
        except Exception:
            continue
    return result


@app.post("/profiles")
async def create_profile(
    audio: UploadFile = File(...),
    transcript: str = Form(...),
    display_name: str = Form("내 목소리"),
    consent: bool = Form(...),
) -> dict[str, Any]:
    transcript = re.sub(r"\s+", " ", transcript).strip()
    display_name = re.sub(r"\s+", " ", display_name).strip()[:80] or "내 목소리"
    if not consent:
        raise HTTPException(status_code=400, detail="본인 또는 명시적으로 허가받은 목소리만 등록할 수 있습니다.")
    if len(transcript) < 5 or len(transcript) > 1000:
        raise HTTPException(status_code=400, detail="참조 대본은 5~1000자여야 합니다.")

    workdir = Path(tempfile.mkdtemp(prefix="voice-enroll-"))
    try:
        source = await _store_upload(audio, workdir)
        normalized = workdir / "reference.wav"
        _normalize_audio(source, normalized)
        seconds = _audio_seconds(normalized)
        audio_quality = _audio_stats(normalized)
        if seconds < MIN_SECONDS or seconds > MAX_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=f"참조 음성은 {MIN_SECONDS:.0f}~{MAX_SECONDS:.0f}초여야 합니다. 현재 {seconds:.1f}초입니다.",
            )
        digest = _sha256(normalized)
        profile_id = f"voice-{digest[:12]}-{uuid.uuid4().hex[:6]}"
        destination = PROFILE_DIR / profile_id
        destination.mkdir(parents=True, exist_ok=False)
        shutil.move(str(normalized), str(destination / "reference.wav"))
        metadata = {
            "profile_id": profile_id,
            "display_name": display_name,
            "transcript": transcript,
            "seconds": round(seconds, 3),
            "sample_rate": 24000,
            "sha256": digest,
            "created_at": int(time.time()),
            "consent_recorded": True,
            "audio_quality": audio_quality,
        }
        (destination / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return metadata
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/profiles/{profile_id}/warmup")
def warm_profile(profile_id: str) -> dict[str, Any]:
    started = time.perf_counter()
    profile = _read_profile(profile_id)
    _, backend = _load_model()
    if backend == "faster-qwen3-tts":
        # The faster backend caches reference features lazily on generation. Consume
        # a tiny discarded utterance now so the first real conversational turn does
        # not pay that setup cost.
        for _pcm, _sample_rate, _timing in _iter_audio(
            profile, "네.", "Korean", max(DEFAULT_CHUNK_SIZE, 8)
        ):
            pass
    else:
        _get_prompt(profile)
    return {
        "ok": True,
        "profile_id": profile_id,
        "backend": backend,
        "seconds": time.perf_counter() - started,
    }


@app.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str) -> dict[str, Any]:
    path = _profile_path(profile_id)
    profile = _read_profile(profile_id)
    key = _prompt_key(profile)
    with _model_lock:
        _prompt_cache.pop(key, None)
    shutil.rmtree(path)
    return {"ok": True, "profile_id": profile_id}


@app.post("/synthesize")
def synthesize(payload: dict[str, Any]) -> Response:
    profile = _read_profile(str(payload.get("profile_id", "")))
    text = re.sub(r"\s+", " ", str(payload.get("text", ""))).strip()
    language = str(payload.get("language") or "Korean")
    chunk_size = int(payload.get("chunk_size") or DEFAULT_CHUNK_SIZE)
    if not text or len(text) > 1000:
        raise HTTPException(status_code=400, detail="합성 문장은 1~1000자여야 합니다.")
    chunks: list[bytes] = []
    sample_rate = 24000
    for pcm, sample_rate, _ in _iter_audio(profile, text, language, chunk_size):
        chunks.append(pcm)
    return Response(_wav_bytes(chunks, sample_rate), media_type="audio/wav")


@app.websocket("/ws/synthesize")
async def synthesize_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    stop = threading.Event()
    worker: threading.Thread | None = None
    try:
        raw = await websocket.receive_text()
        request = json.loads(raw)
        if request.get("type") != "synthesize":
            raise ValueError("first websocket message must be synthesize")
        request_id = str(request.get("request_id") or uuid.uuid4().hex)
        profile = _read_profile(str(request.get("profile_id", "")))
        text = re.sub(r"\s+", " ", str(request.get("text", ""))).strip()
        language = str(request.get("language") or "Korean")
        chunk_size = max(1, min(24, int(request.get("chunk_size") or DEFAULT_CHUNK_SIZE)))
        if not text or len(text) > 1000:
            raise ValueError("합성 문장은 1~1000자여야 합니다.")

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        def produce() -> None:
            started = time.perf_counter()
            sent_start = False
            chunks = 0
            try:
                for pcm, sample_rate, timing in _iter_audio(profile, text, language, chunk_size):
                    if stop.is_set():
                        break
                    if not sent_start:
                        sent_start = True
                        loop.call_soon_threadsafe(
                            queue.put_nowait,
                            ("start", {"sample_rate": sample_rate, "timing": timing}),
                        )
                    chunks += 1
                    loop.call_soon_threadsafe(queue.put_nowait, ("audio", (pcm, timing)))
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    ("done", {"chunks": chunks, "seconds": time.perf_counter() - started}),
                )
            except Exception as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    ("error", f"{type(exc).__name__}: {exc}"),
                )

        worker = threading.Thread(target=produce, daemon=True, name=f"tts-{request_id[:12]}")
        worker.start()

        while True:
            event_type, value = await queue.get()
            if event_type == "start":
                await websocket.send_json({"type": "start", "request_id": request_id, **value})
            elif event_type == "audio":
                pcm, timing = value
                await websocket.send_json({"type": "chunk", "request_id": request_id, "timing": timing})
                await websocket.send_bytes(pcm)
            elif event_type == "done":
                await websocket.send_json({"type": "done", "request_id": request_id, **value})
                return
            elif event_type == "error":
                await websocket.send_json({"type": "error", "request_id": request_id, "message": value})
                return
    except WebSocketDisconnect:
        stop.set()
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
    finally:
        stop.set()
        if worker is not None and worker.is_alive():
            worker.join(timeout=0.2)
        try:
            await websocket.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, workers=1)
