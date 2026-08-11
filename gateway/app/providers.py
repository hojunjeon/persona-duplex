from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import struct
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx
from websockets.asyncio.client import connect


class ProviderError(RuntimeError):
    pass


class StreamingSTT(ABC):
    events: asyncio.Queue[dict[str, Any]]

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def send_audio(self, pcm16: bytes) -> None: ...

    @abstractmethod
    async def reset(self) -> None: ...

    @abstractmethod
    async def finish(self, utterance_id: str) -> str: ...

    @abstractmethod
    async def close(self) -> None: ...


class QwenSTTWebSocket(StreamingSTT):
    """Client for the bundled Qwen3-ASR streaming microservice."""

    def __init__(self, url: str, language: str) -> None:
        self.url = url
        self.language = language
        self.events = asyncio.Queue()
        self._ws = None
        self._receiver: asyncio.Task | None = None
        self._final_waiters: dict[str, asyncio.Future[str]] = {}
        self._send_lock = asyncio.Lock()

    async def start(self) -> None:
        self._ws = await connect(self.url, max_size=None, ping_interval=20, ping_timeout=20)
        await self._ws.send(json.dumps({"type": "configure", "language": self.language}))
        self._receiver = asyncio.create_task(self._receive_loop(), name="qwen-stt-receiver")

    async def send_audio(self, pcm16: bytes) -> None:
        if not pcm16 or self._ws is None:
            return
        async with self._send_lock:
            await self._ws.send(pcm16)

    async def reset(self) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            await self._ws.send(json.dumps({"type": "reset"}))

    async def finish(self, utterance_id: str) -> str:
        if self._ws is None:
            raise ProviderError("STT websocket is not connected")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._final_waiters[utterance_id] = future
        async with self._send_lock:
            await self._ws.send(json.dumps({"type": "finish", "utterance_id": utterance_id}))
        try:
            return await asyncio.wait_for(future, timeout=45)
        finally:
            self._final_waiters.pop(utterance_id, None)

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for payload in self._ws:
                if not isinstance(payload, str):
                    continue
                event = json.loads(payload)
                if event.get("type") == "final":
                    utterance_id = str(event.get("utterance_id", ""))
                    waiter = self._final_waiters.get(utterance_id)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(str(event.get("text", "")).strip())
                await self.events.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = ProviderError(f"STT connection failed: {type(exc).__name__}: {exc}")
            for waiter in self._final_waiters.values():
                if not waiter.done():
                    waiter.set_exception(error)
            await self.events.put({"type": "error", "source": "stt", "message": str(error)})

    async def close(self) -> None:
        if self._receiver is not None:
            self._receiver.cancel()
            await asyncio.gather(self._receiver, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()
        self._ws = None


class ElevenLabsRealtimeSTT(StreamingSTT):
    """Scribe v2 Realtime adapter using manual commits at browser-detected turn ends."""

    BASE_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"

    def __init__(self, *, api_key: str, model: str, language_code: str) -> None:
        if not api_key:
            raise ValueError("STT_API_KEY or ELEVENLABS_API_KEY is required for elevenlabs_ws")
        self.api_key = api_key
        self.model = model
        self.language_code = language_code
        self.events = asyncio.Queue()
        self._ws = None
        self._receiver: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._audio_buffer = bytearray()
        self._ready = asyncio.Event()
        self._finish_waiter: asyncio.Future[str] | None = None
        self._finish_utterance_id = ""
        self._latest_final = ""
        self._closed = False

    async def start(self) -> None:
        query = urlencode(
            {
                "model_id": self.model,
                "audio_format": "pcm_16000",
                "language_code": self.language_code,
                "commit_strategy": "manual",
                "include_timestamps": "false",
                "no_verbatim": "false",
            }
        )
        self._ws = await connect(
            f"{self.BASE_URL}?{query}",
            additional_headers={"xi-api-key": self.api_key},
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        )
        self._receiver = asyncio.create_task(self._receive_loop(), name="elevenlabs-stt-receiver")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=15)
        except TimeoutError as exc:
            raise ProviderError("ElevenLabs STT session did not become ready") from exc

    async def send_audio(self, pcm16: bytes) -> None:
        if not pcm16 or self._ws is None:
            return
        self._audio_buffer.extend(pcm16)
        # Keep the newest 100 ms for the explicit commit packet. Some realtime
        # endpoints reject an empty ``audio_base_64`` commit, which occurs when an
        # utterance happens to end on an exact batching boundary. Everything older
        # can be streamed immediately for responsive partial transcripts.
        if len(self._audio_buffer) >= 6400:
            flush_bytes = len(self._audio_buffer) - 3200
            payload = bytes(self._audio_buffer[:flush_bytes])
            del self._audio_buffer[:flush_bytes]
            await self._send_audio_event(payload, commit=False)

    async def _send_audio_event(self, pcm16: bytes, *, commit: bool) -> None:
        if self._ws is None:
            raise ProviderError("ElevenLabs STT websocket is not connected")
        event: dict[str, Any] = {
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(pcm16).decode("ascii"),
        }
        if commit:
            event["commit"] = True
        async with self._send_lock:
            await self._ws.send(json.dumps(event))

    async def reset(self) -> None:
        # Manual commit clears the prior segment server-side. reset() prepares the next
        # local segment and intentionally does not reconnect, preserving model context.
        self._audio_buffer.clear()
        self._latest_final = ""

    async def finish(self, utterance_id: str) -> str:
        if self._ws is None:
            raise ProviderError("ElevenLabs STT websocket is not connected")
        if self._finish_waiter is not None and not self._finish_waiter.done():
            raise ProviderError("an ElevenLabs STT commit is already pending")
        self._finish_utterance_id = utterance_id
        self._finish_waiter = asyncio.get_running_loop().create_future()
        payload = bytes(self._audio_buffer)
        self._audio_buffer.clear()
        await self._send_audio_event(payload, commit=True)
        try:
            return await asyncio.wait_for(self._finish_waiter, timeout=30)
        finally:
            self._finish_waiter = None
            self._finish_utterance_id = ""
            self._latest_final = ""

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for payload in self._ws:
                if not isinstance(payload, str):
                    continue
                event = json.loads(payload)
                message_type = str(event.get("message_type", ""))
                text = str(event.get("text", "")).strip()
                if message_type == "session_started":
                    self._ready.set()
                    await self.events.put(
                        {
                            "type": "ready",
                            "engine": "elevenlabs-scribe-v2-realtime",
                            "model": self.model,
                        }
                    )
                elif message_type == "partial_transcript":
                    if text:
                        await self.events.put({"type": "partial", "text": text, "language": self.language_code})
                elif message_type == "final_transcript":
                    if text:
                        self._latest_final = text
                        await self.events.put({"type": "partial", "text": text, "language": self.language_code})
                elif message_type in {"committed_transcript", "committed_transcript_with_timestamps"}:
                    committed = text or self._latest_final
                    waiter = self._finish_waiter
                    if waiter is not None and not waiter.done():
                        waiter.set_result(committed)
                    await self.events.put(
                        {
                            "type": "final",
                            "utterance_id": self._finish_utterance_id,
                            "text": committed,
                            "language": self.language_code,
                        }
                    )
                elif message_type.endswith("error") or event.get("error"):
                    raise ProviderError(str(event.get("error") or event))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, ProviderError) else ProviderError(f"ElevenLabs STT failed: {exc}")
            if self._finish_waiter is not None and not self._finish_waiter.done():
                self._finish_waiter.set_exception(error)
            self._ready.set()
            await self.events.put({"type": "error", "source": "stt", "message": str(error)})

    async def close(self) -> None:
        self._closed = True
        if self._receiver is not None:
            self._receiver.cancel()
            await asyncio.gather(self._receiver, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()
        self._ws = None


class SonioxRealtimeSTT(StreamingSTT):
    """Soniox stt-rt-v5 adapter with client-side VAD and manual finalization."""

    BASE_URL = "wss://stt-rt.soniox.com/transcribe-websocket"

    def __init__(self, *, api_key: str, model: str, language_code: str) -> None:
        if not api_key:
            raise ValueError("STT_API_KEY or SONIOX_API_KEY is required for soniox_ws")
        self.api_key = api_key
        self.model = model
        self.language_code = language_code
        self.events = asyncio.Queue()
        self._ws = None
        self._receiver: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._audio_buffer = bytearray()
        self._final_tokens: list[str] = []
        self._latest_preview = ""
        self._finish_waiter: asyncio.Future[str] | None = None
        self._finish_utterance_id = ""
        self._finalizing = False
        self._last_send_at = 0.0

    async def start(self) -> None:
        self._ws = await connect(self.BASE_URL, max_size=None, ping_interval=20, ping_timeout=20)
        config = {
            "api_key": self.api_key,
            "model": self.model,
            "audio_format": "pcm_s16le",
            "sample_rate": 16000,
            "num_channels": 1,
            "language_hints": [self.language_code],
            # The browser owns turn detection so a single consistent VAD drives
            # interruption, transcript commits, and LLM cancellation.
            "enable_endpoint_detection": False,
        }
        await self._ws.send(json.dumps(config))
        self._last_send_at = asyncio.get_running_loop().time()
        self._receiver = asyncio.create_task(self._receive_loop(), name="soniox-stt-receiver")
        self._keepalive = asyncio.create_task(self._keepalive_loop(), name="soniox-stt-keepalive")
        await self.events.put({"type": "ready", "engine": "soniox-realtime", "model": self.model})

    async def send_audio(self, pcm16: bytes) -> None:
        if not pcm16 or self._ws is None:
            return
        self._audio_buffer.extend(pcm16)
        # 60 ms frames keep request overhead sensible without hiding partials.
        if len(self._audio_buffer) >= 1920:
            payload = bytes(self._audio_buffer)
            self._audio_buffer.clear()
            async with self._send_lock:
                await self._ws.send(payload)
                self._last_send_at = asyncio.get_running_loop().time()

    async def reset(self) -> None:
        self._audio_buffer.clear()
        self._final_tokens.clear()
        self._latest_preview = ""
        self._finalizing = False

    async def finish(self, utterance_id: str) -> str:
        if self._ws is None:
            raise ProviderError("Soniox STT websocket is not connected")
        if self._finish_waiter is not None and not self._finish_waiter.done():
            raise ProviderError("a Soniox STT finalization is already pending")
        self._finish_utterance_id = utterance_id
        self._finish_waiter = asyncio.get_running_loop().create_future()
        async with self._send_lock:
            if self._audio_buffer:
                await self._ws.send(bytes(self._audio_buffer))
                self._audio_buffer.clear()
            self._finalizing = True
            await self._ws.send(json.dumps({"type": "finalize"}))
            self._last_send_at = asyncio.get_running_loop().time()
        try:
            return await asyncio.wait_for(self._finish_waiter, timeout=30)
        finally:
            self._finish_waiter = None
            self._finish_utterance_id = ""
            self._finalizing = False
            self._final_tokens.clear()
            self._latest_preview = ""

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for payload in self._ws:
                if not isinstance(payload, str):
                    continue
                event = json.loads(payload)
                if event.get("error_code") or event.get("error_type"):
                    raise ProviderError(str(event.get("error_message") or event))

                non_final: list[str] = []
                saw_finalize_marker = False
                for token in event.get("tokens") or []:
                    token_text = str(token.get("text", ""))
                    if token_text == "<fin>":
                        saw_finalize_marker = True
                        continue
                    if token.get("is_final"):
                        # Soniox documents final tokens as emitted exactly once.
                        self._final_tokens.append(token_text)
                    else:
                        non_final.append(token_text)
                preview = ("".join(self._final_tokens) + "".join(non_final)).strip()
                if preview and preview != self._latest_preview:
                    self._latest_preview = preview
                    await self.events.put({"type": "partial", "text": preview, "language": self.language_code})

                final_ms = int(event.get("final_audio_proc_ms") or 0)
                total_ms = int(event.get("total_audio_proc_ms") or 0)
                fully_final = final_ms >= total_ms and not non_final
                waiter = self._finish_waiter
                if self._finalizing and (saw_finalize_marker or fully_final) and waiter is not None and not waiter.done():
                    text = "".join(self._final_tokens).strip()
                    waiter.set_result(text)
                    await self.events.put(
                        {
                            "type": "final",
                            "utterance_id": self._finish_utterance_id,
                            "text": text,
                            "language": self.language_code,
                        }
                    )
                if event.get("finished"):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, ProviderError) else ProviderError(f"Soniox STT failed: {exc}")
            if self._finish_waiter is not None and not self._finish_waiter.done():
                self._finish_waiter.set_exception(error)
            await self.events.put({"type": "error", "source": "stt", "message": str(error)})

    async def _keepalive_loop(self) -> None:
        try:
            while self._ws is not None:
                await asyncio.sleep(8)
                now = asyncio.get_running_loop().time()
                if now - self._last_send_at < 8:
                    continue
                async with self._send_lock:
                    if self._ws is not None:
                        await self._ws.send(json.dumps({"type": "keepalive"}))
                        self._last_send_at = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.events.put({"type": "error", "source": "stt", "message": f"Soniox keepalive failed: {exc}"})

    async def close(self) -> None:
        if self._keepalive is not None:
            self._keepalive.cancel()
            await asyncio.gather(self._keepalive, return_exceptions=True)
        if self._ws is not None:
            try:
                async with self._send_lock:
                    await self._ws.send(b"")
            except Exception:
                pass
        if self._receiver is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._receiver), timeout=1.5)
            except Exception:
                self._receiver.cancel()
                await asyncio.gather(self._receiver, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()
        self._ws = None


class DeepgramRealtimeSTT(StreamingSTT):
    """Deepgram Nova streaming adapter with explicit per-utterance Finalize."""

    BASE_URL = "wss://api.deepgram.com/v1/listen"

    def __init__(self, *, api_key: str, model: str, language_code: str) -> None:
        if not api_key:
            raise ValueError("STT_API_KEY or DEEPGRAM_API_KEY is required for deepgram_ws")
        self.api_key = api_key
        self.model = model
        self.language_code = language_code
        self.events = asyncio.Queue()
        self._ws = None
        self._receiver: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._final_parts: list[str] = []
        self._latest_preview = ""
        self._finish_waiter: asyncio.Future[str] | None = None
        self._finish_utterance_id = ""
        self._finish_fallback: asyncio.Task | None = None
        self._last_send_at = 0.0

    async def start(self) -> None:
        query = urlencode(
            {
                "model": self.model,
                "language": self.language_code,
                "encoding": "linear16",
                "sample_rate": "16000",
                "channels": "1",
                "interim_results": "true",
                "endpointing": "false",
                "smart_format": "true",
            }
        )
        self._ws = await connect(
            f"{self.BASE_URL}?{query}",
            additional_headers={"Authorization": f"Token {self.api_key}"},
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        )
        self._last_send_at = asyncio.get_running_loop().time()
        self._receiver = asyncio.create_task(self._receive_loop(), name="deepgram-stt-receiver")
        self._keepalive = asyncio.create_task(self._keepalive_loop(), name="deepgram-stt-keepalive")
        await self.events.put({"type": "ready", "engine": "deepgram-nova", "model": self.model})

    async def send_audio(self, pcm16: bytes) -> None:
        if not pcm16 or self._ws is None:
            return
        async with self._send_lock:
            await self._ws.send(pcm16)
            self._last_send_at = asyncio.get_running_loop().time()

    async def reset(self) -> None:
        self._final_parts.clear()
        self._latest_preview = ""

    async def finish(self, utterance_id: str) -> str:
        if self._ws is None:
            raise ProviderError("Deepgram STT websocket is not connected")
        if self._finish_waiter is not None and not self._finish_waiter.done():
            raise ProviderError("a Deepgram STT finalization is already pending")
        self._finish_utterance_id = utterance_id
        self._finish_waiter = asyncio.get_running_loop().create_future()
        async with self._send_lock:
            await self._ws.send(json.dumps({"type": "Finalize"}))
            self._last_send_at = asyncio.get_running_loop().time()
        self._finish_fallback = asyncio.create_task(
            self._finish_after_grace(self._finish_waiter),
            name="deepgram-finalize-fallback",
        )
        try:
            return await asyncio.wait_for(self._finish_waiter, timeout=30)
        finally:
            if self._finish_fallback is not None:
                self._finish_fallback.cancel()
                await asyncio.gather(self._finish_fallback, return_exceptions=True)
                self._finish_fallback = None
            self._finish_waiter = None
            self._finish_utterance_id = ""
            self._final_parts.clear()
            self._latest_preview = ""

    async def _finish_after_grace(self, waiter: asyncio.Future[str]) -> None:
        # Deepgram says from_finalize is usually returned, but not guaranteed when
        # little audio is buffered. Give final results a short grace window, then
        # release the turn instead of stalling the whole conversation for 30 s.
        await asyncio.sleep(1.25)
        if waiter.done():
            return
        committed = " ".join(self._final_parts).strip() or self._latest_preview.strip()
        waiter.set_result(committed)
        await self.events.put(
            {
                "type": "final",
                "utterance_id": self._finish_utterance_id,
                "text": committed,
                "language": self.language_code,
                "fallback": True,
            }
        )

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for payload in self._ws:
                if not isinstance(payload, str):
                    continue
                event = json.loads(payload)
                if event.get("type") == "Error" or event.get("error"):
                    raise ProviderError(str(event.get("description") or event.get("error") or event))
                if event.get("type") != "Results":
                    continue
                alternatives = ((event.get("channel") or {}).get("alternatives") or [])
                text = str(alternatives[0].get("transcript", "")).strip() if alternatives else ""
                if text and event.get("is_final"):
                    self._final_parts.append(text)
                    preview = " ".join(self._final_parts).strip()
                else:
                    preview = " ".join([*self._final_parts, text]).strip()
                if preview and preview != self._latest_preview:
                    self._latest_preview = preview
                    await self.events.put({"type": "partial", "text": preview, "language": self.language_code})
                if event.get("from_finalize"):
                    committed = " ".join(self._final_parts).strip()
                    waiter = self._finish_waiter
                    if waiter is not None and not waiter.done():
                        waiter.set_result(committed)
                    await self.events.put(
                        {
                            "type": "final",
                            "utterance_id": self._finish_utterance_id,
                            "text": committed,
                            "language": self.language_code,
                        }
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, ProviderError) else ProviderError(f"Deepgram STT failed: {exc}")
            if self._finish_waiter is not None and not self._finish_waiter.done():
                self._finish_waiter.set_exception(error)
            await self.events.put({"type": "error", "source": "stt", "message": str(error)})

    async def _keepalive_loop(self) -> None:
        try:
            while self._ws is not None:
                await asyncio.sleep(5)
                now = asyncio.get_running_loop().time()
                if now - self._last_send_at < 5:
                    continue
                async with self._send_lock:
                    if self._ws is not None:
                        await self._ws.send(json.dumps({"type": "KeepAlive"}))
                        self._last_send_at = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.events.put({"type": "error", "source": "stt", "message": f"Deepgram keepalive failed: {exc}"})

    async def close(self) -> None:
        if self._finish_fallback is not None:
            self._finish_fallback.cancel()
            await asyncio.gather(self._finish_fallback, return_exceptions=True)
            self._finish_fallback = None
        if self._keepalive is not None:
            self._keepalive.cancel()
            await asyncio.gather(self._keepalive, return_exceptions=True)
        if self._ws is not None:
            try:
                async with self._send_lock:
                    await self._ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass
        if self._receiver is not None:
            self._receiver.cancel()
            await asyncio.gather(self._receiver, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()
        self._ws = None


class MockSTT(StreamingSTT):
    def __init__(self, transcript: str) -> None:
        self.transcript = transcript
        self.events = asyncio.Queue()
        self._bytes = 0

    async def start(self) -> None:
        await self.events.put({"type": "ready", "engine": "mock-stt"})

    async def send_audio(self, pcm16: bytes) -> None:
        self._bytes += len(pcm16)
        if self._bytes and self._bytes % 16000 < len(pcm16):
            words = self.transcript.split()
            visible = max(1, min(len(words), self._bytes // 16000))
            await self.events.put({"type": "partial", "text": " ".join(words[:visible]), "language": "Korean"})

    async def reset(self) -> None:
        self._bytes = 0

    async def finish(self, utterance_id: str) -> str:
        text = self.transcript.strip() if self._bytes else ""
        await self.events.put({"type": "final", "utterance_id": utterance_id, "text": text, "language": "Korean"})
        self._bytes = 0
        return text

    async def close(self) -> None:
        return None


class StreamingLLM(ABC):
    @abstractmethod
    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]: ...


class IncrementalThinkFilter:
    """Remove <think>...</think> blocks even when tags are split across SSE chunks."""

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self.buffer = ""
        self.in_think = False

    @staticmethod
    def _suffix_prefix_length(text: str, marker: str) -> int:
        maximum = min(len(text), len(marker) - 1)
        for size in range(maximum, 0, -1):
            if text.endswith(marker[:size]):
                return size
        return 0

    def feed(self, chunk: str) -> list[str]:
        self.buffer += chunk
        output: list[str] = []
        while self.buffer:
            if self.in_think:
                end = self.buffer.find(self.CLOSE)
                if end >= 0:
                    self.buffer = self.buffer[end + len(self.CLOSE) :]
                    self.in_think = False
                    continue
                keep = self._suffix_prefix_length(self.buffer, self.CLOSE)
                self.buffer = self.buffer[-keep:] if keep else ""
                break

            start = self.buffer.find(self.OPEN)
            if start >= 0:
                if start:
                    output.append(self.buffer[:start])
                self.buffer = self.buffer[start + len(self.OPEN) :]
                self.in_think = True
                continue

            keep = self._suffix_prefix_length(self.buffer, self.OPEN)
            visible = self.buffer[:-keep] if keep else self.buffer
            if visible:
                output.append(visible)
            self.buffer = self.buffer[-keep:] if keep else ""
            break
        return output

    def flush(self) -> list[str]:
        if self.in_think:
            self.buffer = ""
            return []
        text = self.buffer
        self.buffer = ""
        return [text] if text else []


class OpenAICompatibleLLM(StreamingLLM):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
    ) -> None:
        self.url = urljoin(base_url.rstrip("/") + "/", "chat/completions")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.extra_body = self._load_extra_body()

    @staticmethod
    def _load_extra_body() -> dict[str, Any]:
        raw = os.getenv("LLM_EXTRA_BODY_JSON", "").strip()
        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("LLM_EXTRA_BODY_JSON must be a JSON object")
        return parsed

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        payload.update(self.extra_body)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(self.timeout_seconds, connect=15)
        think_filter = IncrementalThinkFilter()

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", self.url, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise ProviderError(f"LLM HTTP {response.status_code}: {body[-800:]}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    item = json.loads(data)
                    choices = item.get("choices") or []
                    if not choices:
                        continue
                    content = (choices[0].get("delta") or {}).get("content")
                    if content is None:
                        continue
                    if isinstance(content, list):
                        content = "".join(
                            str(part.get("text", "")) if isinstance(part, dict) else str(part)
                            for part in content
                        )
                    for visible in think_filter.feed(str(content)):
                        if visible:
                            yield visible
                for visible in think_filter.flush():
                    if visible:
                        yield visible


class MockLLM(StreamingLLM):
    def __init__(self, reply: str = "좋아. 네 말을 들었고, 지금부터 자연스러운 실시간 음성 대화 흐름으로 답할게.") -> None:
        self.reply = reply

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        del messages
        for part in self.reply.split(" "):
            await asyncio.sleep(0.025)
            yield part + " "


@dataclass(frozen=True)
class TTSChunk:
    pcm16: bytes
    sample_rate: int
    timing: dict[str, Any]


class StreamingTTS(ABC):
    @abstractmethod
    async def stream(
        self,
        *,
        profile_id: str,
        text: str,
        language: str,
        chunk_size: int,
        request_id: str,
    ) -> AsyncIterator[TTSChunk]: ...


class QwenTTSWebSocket(StreamingTTS):
    def __init__(self, url: str) -> None:
        self.url = url

    async def stream(
        self,
        *,
        profile_id: str,
        text: str,
        language: str,
        chunk_size: int,
        request_id: str,
    ) -> AsyncIterator[TTSChunk]:
        async with connect(self.url, max_size=None, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "synthesize",
                        "request_id": request_id,
                        "profile_id": profile_id,
                        "text": text,
                        "language": language,
                        "chunk_size": chunk_size,
                    },
                    ensure_ascii=False,
                )
            )
            sample_rate: int | None = None
            timing: dict[str, Any] = {}
            async for payload in ws:
                if isinstance(payload, bytes):
                    if sample_rate is None:
                        raise ProviderError("TTS sent audio before stream metadata")
                    yield TTSChunk(payload, sample_rate, timing)
                    continue
                event = json.loads(payload)
                event_type = event.get("type")
                if event_type == "start":
                    sample_rate = int(event["sample_rate"])
                    timing = dict(event.get("timing") or {})
                elif event_type == "chunk":
                    timing = dict(event.get("timing") or timing)
                elif event_type == "done":
                    return
                elif event_type == "error":
                    raise ProviderError(str(event.get("message", "TTS failed")))


class MockTTS(StreamingTTS):
    async def stream(
        self,
        *,
        profile_id: str,
        text: str,
        language: str,
        chunk_size: int,
        request_id: str,
    ) -> AsyncIterator[TTSChunk]:
        del profile_id, language, chunk_size, request_id
        sample_rate = 24000
        duration = max(0.22, min(2.2, len(text) * 0.045))
        total = int(sample_rate * duration)
        block = int(sample_rate * 0.12)
        for start in range(0, total, block):
            count = min(block, total - start)
            values = bytearray()
            for i in range(count):
                t = (start + i) / sample_rate
                envelope = min(1.0, (start + i) / 800) * min(1.0, (total - start - i) / 800)
                sample = int(0.12 * envelope * math.sin(2 * math.pi * 220 * t) * 32767)
                values.extend(struct.pack("<h", sample))
            await asyncio.sleep(0.01)
            yield TTSChunk(bytes(values), sample_rate, {"mock": True})


def create_stt(
    mode: str,
    *,
    url: str,
    language: str,
    mock_transcript: str,
    api_key: str = "",
    cloud_model: str = "scribe_v2_realtime",
    cloud_language_code: str = "ko",
) -> StreamingSTT:
    if mode == "mock":
        return MockSTT(mock_transcript)
    if mode == "qwen_ws":
        return QwenSTTWebSocket(url, language)
    if mode == "elevenlabs_ws":
        return ElevenLabsRealtimeSTT(
            api_key=api_key,
            model=cloud_model,
            language_code=cloud_language_code,
        )
    if mode == "soniox_ws":
        return SonioxRealtimeSTT(
            api_key=api_key,
            model=cloud_model,
            language_code=cloud_language_code,
        )
    if mode == "deepgram_ws":
        return DeepgramRealtimeSTT(
            api_key=api_key,
            model=cloud_model,
            language_code=cloud_language_code,
        )
    raise ValueError(f"unsupported STT_MODE: {mode}")


def create_llm(
    mode: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout_seconds: float,
) -> StreamingLLM:
    if mode == "mock":
        return MockLLM()
    if mode == "openai_compatible":
        return OpenAICompatibleLLM(
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )
    raise ValueError(f"unsupported LLM_MODE: {mode}")


def create_tts(mode: str, *, url: str) -> StreamingTTS:
    if mode == "mock":
        return MockTTS()
    if mode == "qwen_ws":
        return QwenTTSWebSocket(url)
    raise ValueError(f"unsupported TTS_MODE: {mode}")
