from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Any

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

MODEL_ID = os.getenv("ASR_MODEL_ID", "Qwen/Qwen3-ASR-1.7B")
PORT = int(os.getenv("ASR_PORT", "8101"))
GPU_MEMORY_UTILIZATION = float(os.getenv("ASR_GPU_MEMORY_UTILIZATION", "0.45"))
CHUNK_SIZE_SEC = float(os.getenv("ASR_CHUNK_SIZE_SEC", "0.5"))
UNFIXED_CHUNK_NUM = int(os.getenv("ASR_UNFIXED_CHUNK_NUM", "2"))
UNFIXED_TOKEN_NUM = int(os.getenv("ASR_UNFIXED_TOKEN_NUM", "5"))
DEFAULT_LANGUAGE = os.getenv("ASR_LANGUAGE", "Korean")
MAX_NEW_TOKENS = int(os.getenv("ASR_MAX_NEW_TOKENS", "64"))

app = FastAPI(title="Persona Duplex Qwen ASR", version="1.0")
_model: Any = None
_model_error: str | None = None
_model_lock = threading.RLock()


def _device_name() -> str:
    try:
        import torch

        return torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    except Exception:
        return "unknown"


def _load_model() -> Any:
    global _model, _model_error
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from qwen_asr import Qwen3ASRModel

            _model = Qwen3ASRModel.LLM(
                model=MODEL_ID,
                gpu_memory_utilization=GPU_MEMORY_UTILIZATION,
                max_inference_batch_size=1,
                max_new_tokens=MAX_NEW_TOKENS,
            )
            _model_error = None
            return _model
        except Exception as exc:
            _model_error = f"{type(exc).__name__}: {exc}"
            raise


def _new_state(language: str) -> Any:
    model = _load_model()
    with _model_lock:
        return model.init_streaming_state(
            unfixed_chunk_num=UNFIXED_CHUNK_NUM,
            unfixed_token_num=UNFIXED_TOKEN_NUM,
            chunk_size_sec=CHUNK_SIZE_SEC,
            language=language or None,
        )


def _decode_step(state: Any, pcm: np.ndarray) -> tuple[Any, str, str]:
    """Advance the Qwen streaming state in place.

    Qwen3-ASR's official API mutates ``state`` and does not promise a return
    value. Keeping the original object avoids replacing it with ``None`` on
    package versions that follow that contract exactly.
    """
    model = _load_model()
    with _model_lock:
        model.streaming_transcribe(pcm, state)
        return state, str(state.language or ""), str(state.text or "")


def _finish(state: Any, tail: np.ndarray | None) -> tuple[Any, str, str]:
    """Flush pending samples and finalize the same mutable streaming state."""
    model = _load_model()
    with _model_lock:
        if tail is not None and tail.size:
            model.streaming_transcribe(tail, state)
        model.finish_streaming_transcribe(state)
        return state, str(state.language or ""), str(state.text or "")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": _model_error is None,
        "model": MODEL_ID,
        "loaded": _model is not None,
        "device": _device_name(),
        "chunk_size_sec": CHUNK_SIZE_SEC,
        "error": _model_error,
    }


@app.post("/warmup")
def warmup() -> dict[str, Any]:
    started = time.perf_counter()
    _load_model()
    return {"ok": True, "seconds": time.perf_counter() - started}


@app.websocket("/ws")
async def websocket_asr(websocket: WebSocket) -> None:
    await websocket.accept()
    language = DEFAULT_LANGUAGE
    state = None
    pending = bytearray()
    step_bytes = max(3200, int(16000 * CHUNK_SIZE_SEC) * 2)
    last_partial = ""

    async def reset() -> None:
        nonlocal state, pending, last_partial
        state = await asyncio.to_thread(_new_state, language)
        pending = bytearray()
        last_partial = ""

    try:
        await reset()
        await websocket.send_json({"type": "ready", "engine": "qwen3-asr", "model": MODEL_ID})
        while True:
            message = await websocket.receive()
            if message.get("bytes") is not None:
                pending.extend(message["bytes"])
                while len(pending) >= step_bytes:
                    raw = bytes(pending[:step_bytes])
                    del pending[:step_bytes]
                    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                    state, detected_language, text = await asyncio.to_thread(_decode_step, state, pcm)
                    if text and text != last_partial:
                        last_partial = text
                        await websocket.send_json(
                            {"type": "partial", "text": text, "language": detected_language or language}
                        )
                continue

            raw_text = message.get("text")
            if raw_text is None:
                continue
            event = json.loads(raw_text)
            event_type = event.get("type")
            if event_type == "configure":
                language = str(event.get("language") or DEFAULT_LANGUAGE)
                await reset()
                await websocket.send_json({"type": "configured", "language": language})
            elif event_type == "reset":
                await reset()
                await websocket.send_json({"type": "reset"})
            elif event_type == "finish":
                utterance_id = str(event.get("utterance_id") or "")
                tail = None
                if pending:
                    even_length = len(pending) - (len(pending) % 2)
                    tail = np.frombuffer(bytes(pending[:even_length]), dtype="<i2").astype(np.float32) / 32768.0
                state, detected_language, text = await asyncio.to_thread(_finish, state, tail)
                await websocket.send_json(
                    {
                        "type": "final",
                        "utterance_id": utterance_id,
                        "text": text.strip(),
                        "language": detected_language or language,
                    }
                )
                await reset()
            else:
                await websocket.send_json({"type": "warning", "message": f"unknown event: {event_type}"})
    except WebSocketDisconnect:
        return
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, workers=1)
