from __future__ import annotations

import argparse
import asyncio
import csv
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmark.metrics import cer, wer  # noqa: E402
from gateway.app.providers import (  # noqa: E402
    DeepgramRealtimeSTT,
    ElevenLabsRealtimeSTT,
    QwenSTTWebSocket,
    SonioxRealtimeSTT,
    StreamingSTT,
)


@dataclass
class Result:
    provider: str
    audio_path: str
    reference: str
    transcript: str = ""
    audio_seconds: float = 0.0
    first_partial_ms: float | None = None
    final_after_end_ms: float | None = None
    total_ms: float | None = None
    rtf: float | None = None
    cer: float | None = None
    wer: float | None = None
    error: str = ""


def pcm16_16k(path: Path) -> tuple[bytes, float]:
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
    ]
    completed = subprocess.run(command, capture_output=True, check=False, timeout=180)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", "replace")[-800:])
    pcm = completed.stdout
    return pcm, len(pcm) / 2 / 16000


async def benchmark_gateway_provider(
    name: str,
    provider: StreamingSTT,
    pcm: bytes,
    duration: float,
    *,
    pace: bool,
) -> tuple[str, float | None, float, float]:
    await provider.start()
    started = time.perf_counter()
    first_partial: float | None = None
    stop = asyncio.Event()

    async def watch() -> None:
        nonlocal first_partial
        while not stop.is_set():
            try:
                event = await asyncio.wait_for(provider.events.get(), timeout=0.2)
            except TimeoutError:
                continue
            if event.get("type") == "partial" and first_partial is None and str(event.get("text", "")).strip():
                first_partial = (time.perf_counter() - started) * 1000

    watcher = asyncio.create_task(watch())
    try:
        frame_bytes = 640  # 20 ms, PCM16 mono 16 kHz
        for offset in range(0, len(pcm), frame_bytes):
            await provider.send_audio(pcm[offset : offset + frame_bytes])
            if pace:
                await asyncio.sleep(0.02)
        audio_end = time.perf_counter()
        transcript = await provider.finish(f"bench-{time.time_ns()}")
        finished = time.perf_counter()
        return (
            transcript,
            first_partial,
            (finished - audio_end) * 1000,
            (finished - started) * 1000,
        )
    finally:
        stop.set()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await provider.close()



def benchmark_faster_whisper(path: Path, model_name: str) -> tuple[str, None, float, float]:
    from faster_whisper import WhisperModel

    device = os.getenv("FASTER_WHISPER_DEVICE", "cuda")
    compute_type = os.getenv("FASTER_WHISPER_COMPUTE_TYPE", "float16")
    started = time.perf_counter()
    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    segments, _ = model.transcribe(str(path), language="ko", beam_size=5, vad_filter=True)
    text = " ".join(segment.text.strip() for segment in segments).strip()
    finished = time.perf_counter()
    return text, None, 0.0, (finished - started) * 1000


async def benchmark_openai(path: Path) -> tuple[str, None, float, float]:
    import httpx

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing")
    model = os.getenv("OPENAI_STT_MODEL", "gpt-4o-transcribe")
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=180) as client:
        with path.open("rb") as handle:
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                data={"model": model, "language": "ko", "response_format": "json"},
                files={"file": (path.name, handle, "application/octet-stream")},
            )
    if response.status_code >= 400:
        raise RuntimeError(f"OpenAI HTTP {response.status_code}: {response.text[-800:]}")
    finished = time.perf_counter()
    return str(response.json().get("text", "")).strip(), None, 0.0, (finished - started) * 1000


async def run_one(provider_name: str, path: Path, reference: str, pace: bool, fw_model: str) -> Result:
    pcm, duration = pcm16_16k(path)
    result = Result(provider=provider_name, audio_path=str(path), reference=reference, audio_seconds=round(duration, 3))
    try:
        if provider_name == "qwen":
            provider = QwenSTTWebSocket(os.getenv("QWEN_ASR_WS_URL", "ws://localhost:8101/ws"), "Korean")
            values = await benchmark_gateway_provider(provider_name, provider, pcm, duration, pace=pace)
        elif provider_name == "elevenlabs":
            provider = ElevenLabsRealtimeSTT(
                api_key=os.getenv("ELEVENLABS_API_KEY", ""),
                model=os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2_realtime"),
                language_code="ko",
            )
            values = await benchmark_gateway_provider(provider_name, provider, pcm, duration, pace=pace)
        elif provider_name == "soniox":
            provider = SonioxRealtimeSTT(
                api_key=os.getenv("SONIOX_API_KEY", ""),
                model=os.getenv("SONIOX_STT_MODEL", "stt-rt-v5"),
                language_code="ko",
            )
            values = await benchmark_gateway_provider(provider_name, provider, pcm, duration, pace=pace)
        elif provider_name == "deepgram":
            provider = DeepgramRealtimeSTT(
                api_key=os.getenv("DEEPGRAM_API_KEY", ""),
                model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
                language_code="ko",
            )
            values = await benchmark_gateway_provider(provider_name, provider, pcm, duration, pace=pace)
        elif provider_name == "faster-whisper":
            values = await asyncio.to_thread(benchmark_faster_whisper, path, fw_model)
        elif provider_name == "openai":
            values = await benchmark_openai(path)
        else:
            raise RuntimeError(f"unsupported provider: {provider_name}")
        transcript, first_partial, final_after_end, total = values
        result.transcript = transcript
        result.first_partial_ms = round(first_partial, 1) if first_partial is not None else None
        result.final_after_end_ms = round(final_after_end, 1)
        result.total_ms = round(total, 1)
        result.rtf = round(total / 1000 / max(duration, 0.001), 4)
        result.cer = round(cer(reference, transcript), 5)
        result.wer = round(wer(reference, transcript), 5)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def load_manifest(path: Path) -> list[tuple[Path, str]]:
    rows: list[tuple[Path, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for item in csv.DictReader(handle):
            audio = Path(item["audio_path"])
            if not audio.is_absolute():
                audio = (path.parent / audio).resolve()
            rows.append((audio, item["reference"].strip()))
    if not rows:
        raise RuntimeError("manifest is empty")
    return rows


async def main() -> int:
    parser = argparse.ArgumentParser(description="Korean STT accuracy/latency benchmark")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--providers", default="qwen,elevenlabs,soniox,deepgram,faster-whisper,openai")
    parser.add_argument("--output", type=Path, default=Path("benchmark_results.csv"))
    parser.add_argument("--no-pace", action="store_true", help="send streaming audio faster than real time")
    parser.add_argument("--faster-whisper-model", default="large-v3")
    args = parser.parse_args()

    rows = load_manifest(args.manifest)
    providers = [item.strip() for item in args.providers.split(",") if item.strip()]
    results: list[Result] = []
    for provider in providers:
        for audio_path, reference in rows:
            print(f"[{provider}] {audio_path.name}", flush=True)
            result = await run_one(provider, audio_path, reference, not args.no_pace, args.faster_whisper_model)
            results.append(result)
            if result.error:
                print(f"  ERROR {result.error}")
            else:
                print(f"  CER={result.cer:.3f} final={result.final_after_end_ms:.0f} ms | {result.transcript}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(results[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(item) for item in results)
    print(f"saved: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
