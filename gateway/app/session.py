from __future__ import annotations

import asyncio
import contextlib
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from .config import Settings
from .persona import Persona, build_system_prompt, load_persona
from .protocol import CHANNEL_BACKCHANNEL, CHANNEL_MAIN, pack_audio_frame
from .providers import StreamingLLM, StreamingSTT, StreamingTTS
from .text import ClauseBuffer, is_short_backchannel, sanitize_spoken_text


@dataclass
class PendingAssistantTurn:
    turn_id: str
    started_at: float = field(default_factory=time.perf_counter)
    streams: list[int] = field(default_factory=list)
    clauses: dict[int, str] = field(default_factory=dict)
    played: set[int] = field(default_factory=set)
    interrupted: bool = False
    llm_first_token_ms: float | None = None
    first_audio_ms: float | None = None

    def played_text(self) -> str:
        return " ".join(self.clauses[sid] for sid in self.streams if sid in self.played).strip()


class VoiceSession:
    def __init__(
        self,
        websocket: WebSocket,
        *,
        settings: Settings,
        stt: StreamingSTT,
        llm: StreamingLLM,
        tts: StreamingTTS,
    ) -> None:
        self.websocket = websocket
        self.settings = settings
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.persona: Persona = load_persona(
            settings.persona_dir,
            settings.default_persona,
            settings.persona_data_dir,
        )
        self._persona_configured = False
        self.voice_profile_id = ""
        self.history: list[dict[str, str]] = []

        self._send_lock = asyncio.Lock()
        self._closed = False
        self._stt_events_task: asyncio.Task | None = None
        self._assistant_task: asyncio.Task | None = None
        self._barge_task: asyncio.Task | None = None
        self._backchannel_task: asyncio.Task | None = None
        self._finalize_tasks: set[asyncio.Task] = set()
        self._stt_turn_lock = asyncio.Lock()
        self._pending_finalizations = 0
        self._buffer_current_stt = False
        self._current_stt_audio = bytearray()
        self._max_deferred_stt_bytes = 16000 * 2 * 30
        self._utterance_sequence = 0

        self._user_speaking = False
        self._speech_started_at = 0.0
        self._assistant_playing = False
        self._barge_triggered = False
        self._latest_partial = ""
        self._last_backchannel_at = 0.0
        pre_roll_packets = max(1, round(settings.pre_roll_ms / 20))
        self._pre_roll: deque[bytes] = deque(maxlen=pre_roll_packets)
        self._stream_seq = 0
        self._turns: dict[str, PendingAssistantTurn] = {}
        self._stream_to_turn: dict[int, str] = {}
        self._active_turn_id: str | None = None

    def _assistant_active(self) -> bool:
        return self._assistant_task is not None and not self._assistant_task.done()

    async def start(self) -> None:
        await self.stt.start()
        self._stt_events_task = asyncio.create_task(self._stt_events_loop(), name="stt-events")
        await self.send_json(
            {
                "type": "session.ready",
                "persona": self.persona.persona_id,
                "persona_name": self.persona.name,
                "requires_voice_profile": self.settings.tts_mode != "mock",
            }
        )
        await self._set_state("idle")

    async def handle_audio(self, pcm16: bytes) -> None:
        if self._closed or not pcm16:
            return
        self._pre_roll.append(pcm16)
        if self._user_speaking:
            if self._buffer_current_stt:
                self._current_stt_audio.extend(pcm16)
                if len(self._current_stt_audio) > self._max_deferred_stt_bytes:
                    del self._current_stt_audio[: len(self._current_stt_audio) - self._max_deferred_stt_bytes]
            else:
                await self.stt.send_audio(pcm16)

    async def handle_control(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        if event_type == "session.configure":
            await self._configure(event)
        elif event_type == "client.speech_start":
            await self._speech_start()
        elif event_type == "client.speech_end":
            await self._speech_end()
        elif event_type == "audio.played":
            await self._audio_played(int(event.get("stream_id", -1)))
        elif event_type == "assistant.playback_idle":
            await self._playback_idle(str(event.get("turn_id") or ""))
        elif event_type in {"client.stop", "assistant.cancel"}:
            await self.interrupt_assistant(reason="manual")
        elif event_type == "client.ping":
            await self.send_json({"type": "server.pong", "at": time.time()})
        else:
            await self.send_json({"type": "warning", "message": f"알 수 없는 제어 메시지: {event_type}"})

    async def _configure(self, event: dict[str, Any]) -> None:
        if self._persona_configured:
            await self.send_json(
                {
                    "type": "error",
                    "source": "persona",
                    "message": "대화 중에는 페르소나를 바꿀 수 없습니다.",
                }
            )
            return
        persona_id = str(event.get("persona_id") or self.settings.default_persona)
        profile_id = str(event.get("voice_profile_id") or "").strip()
        self.persona = load_persona(
            self.settings.persona_dir,
            persona_id,
            self.settings.persona_data_dir,
        )
        self.voice_profile_id = profile_id
        self._persona_configured = True
        await self.send_json(
            {
                "type": "session.configured",
                "persona": self.persona.persona_id,
                "persona_name": self.persona.name,
                "voice_profile_id": self.voice_profile_id,
            }
        )

    async def _speech_start(self) -> None:
        if self._user_speaking:
            return
        self._user_speaking = True
        self._speech_started_at = time.monotonic()
        self._barge_triggered = False
        self._latest_partial = ""
        self._buffer_current_stt = self._pending_finalizations > 0
        self._current_stt_audio = bytearray()
        if self._buffer_current_stt:
            for packet in list(self._pre_roll):
                self._current_stt_audio.extend(packet)
        else:
            await self.stt.reset()
            for packet in list(self._pre_roll):
                await self.stt.send_audio(packet)

        # Speech may arrive while audio is playing or while the LLM/TTS is merely
        # preparing its first chunk. Both are interruptions; only playback needs ducking.
        if self._assistant_active() or self._assistant_playing:
            if self._assistant_playing:
                await self.send_json({"type": "audio.duck", "factor": 0.18, "attack_ms": 45})
            self._barge_task = asyncio.create_task(self._confirm_barge_in(), name="barge-confirm")
            await self._set_state("listening")
        else:
            await self._set_state("listening")

        if self.settings.backchannel_enabled:
            self._backchannel_task = asyncio.create_task(self._backchannel_loop(), name="backchannel-loop")

    async def _speech_end(self) -> None:
        if not self._user_speaking:
            return
        self._user_speaking = False
        duration_ms = int((time.monotonic() - self._speech_started_at) * 1000)
        await self._cancel_task(self._backchannel_task)
        self._backchannel_task = None

        assistant_active = self._assistant_active() or self._assistant_playing
        looks_like_ack = (
            duration_ms <= self.settings.short_backchannel_max_ms
            and bool(self._latest_partial)
            and is_short_backchannel(self._latest_partial)
        ) or (not self._latest_partial and duration_ms <= self.settings.empty_backchannel_max_ms)
        if assistant_active and not self._barge_triggered and looks_like_ack:
            await self._cancel_task(self._barge_task)
            self._barge_task = None
            # A previous utterance may still be finalizing under the shared STT lock.
            # Resetting here would erase that utterance's decoder state. Deferred
            # acknowledgements only live in the local buffer, so discard that buffer
            # and leave the provider untouched until the earlier finalization ends.
            if not self._buffer_current_stt:
                await self.stt.reset()
            self._buffer_current_stt = False
            self._current_stt_audio = bytearray()
            await self.send_json({"type": "audio.unduck", "release_ms": 110})
            await self.send_json(
                {
                    "type": "user.backchannel",
                    "duration_ms": duration_ms,
                    "recognized": self._latest_partial,
                }
            )
            self._latest_partial = ""
            return

        await self._cancel_task(self._barge_task)
        self._barge_task = None
        if assistant_active and not self._barge_triggered:
            await self.interrupt_assistant(reason="speech_end_barge")

        utterance_id = uuid.uuid4().hex
        self._utterance_sequence += 1
        utterance_sequence = self._utterance_sequence
        speech_end_perf = time.perf_counter()
        buffered_audio = bytes(self._current_stt_audio) if self._buffer_current_stt else None
        self._buffer_current_stt = False
        self._current_stt_audio = bytearray()
        self._pending_finalizations += 1
        task = asyncio.create_task(
            self._finalize_user_turn(utterance_id, utterance_sequence, speech_end_perf, buffered_audio),
            name=f"finalize-{utterance_id[:8]}",
        )
        self._finalize_tasks.add(task)
        task.add_done_callback(self._finalize_tasks.discard)

    async def _confirm_barge_in(self) -> None:
        await asyncio.sleep(self.settings.barge_in_confirm_ms / 1000)
        if not self._user_speaking or not (self._assistant_active() or self._assistant_playing):
            return

        # Qwen streaming partials usually arrive around the first configured ASR chunk.
        # Give an empty partial a tiny grace period before deciding it is substantive speech.
        if not self._latest_partial:
            await asyncio.sleep(0.12)
            if not self._user_speaking or not (self._assistant_active() or self._assistant_playing):
                return

        if self._latest_partial and is_short_backchannel(self._latest_partial):
            elapsed_ms = int((time.monotonic() - self._speech_started_at) * 1000)
            remaining_ms = max(0, self.settings.short_backchannel_max_ms - elapsed_ms)
            if remaining_ms:
                await asyncio.sleep(remaining_ms / 1000)
            if not self._user_speaking or not (self._assistant_active() or self._assistant_playing):
                return

        self._barge_triggered = True
        await self.interrupt_assistant(reason="barge_in")

    async def interrupt_assistant(self, *, reason: str) -> None:
        turn_id = self._active_turn_id
        if turn_id and turn_id in self._turns:
            self._turns[turn_id].interrupted = True
        task = self._assistant_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._assistant_task = None
        self._assistant_playing = False
        await self.send_json({"type": "assistant.interrupted", "reason": reason, "turn_id": turn_id})
        await self.send_json({"type": "audio.stop", "channel": CHANNEL_MAIN, "turn_id": turn_id})
        await self.send_json({"type": "audio.unduck", "release_ms": 30})
        await self._set_state("listening" if self._user_speaking else "idle")

    async def _finalize_user_turn(
        self,
        utterance_id: str,
        utterance_sequence: int,
        speech_end_perf: float,
        buffered_audio: bytes | None,
    ) -> None:
        try:
            async with self._stt_turn_lock:
                if buffered_audio is not None:
                    await self.stt.reset()
                    for offset in range(0, len(buffered_audio), 640):
                        await self.stt.send_audio(buffered_audio[offset : offset + 640])
                text = (await self.stt.finish(utterance_id)).strip()
            stt_final_ms = round((time.perf_counter() - speech_end_perf) * 1000, 1)
            await self.send_json(
                {
                    "type": "metrics.stt_final",
                    "utterance_id": utterance_id,
                    "milliseconds": stt_final_ms,
                }
            )
            self._latest_partial = ""
            if not text:
                await self.send_json({"type": "transcript.empty"})
                await self._set_state("idle")
                return
            await self.send_json({"type": "transcript.final", "text": text, "utterance_id": utterance_id})
            self.history.append({"role": "user", "content": text})
            self.history = self.history[-self.settings.max_history_messages :]

            # If the user has already started or completed a newer utterance, this
            # transcript is still committed to history but must not trigger a stale
            # response. The newest finalization will answer the accumulated context.
            if utterance_sequence != self._utterance_sequence or self._user_speaking:
                await self.send_json(
                    {
                        "type": "assistant.deferred",
                        "utterance_id": utterance_id,
                        "reason": "newer_user_audio",
                    }
                )
                await self._set_state("listening" if self._user_speaking else "idle")
                return

            self._assistant_task = asyncio.create_task(self._respond(), name=f"assistant-{utterance_id[:8]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._send_error("user_turn", exc)
            await self._set_state("idle")
        finally:
            self._pending_finalizations = max(0, self._pending_finalizations - 1)

    async def _respond(self) -> None:
        if not self.voice_profile_id and self.settings.tts_mode != "mock":
            await self.send_json(
                {"type": "error", "source": "voice", "message": "먼저 목소리 프로필을 녹음해 등록해야 합니다."}
            )
            await self._set_state("idle")
            return

        turn_id = uuid.uuid4().hex
        turn = PendingAssistantTurn(turn_id)
        self._turns[turn_id] = turn
        self._active_turn_id = turn_id
        clause_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=3)
        buffer = ClauseBuffer(max_chars=self.settings.max_clause_chars, min_chars=self.settings.min_clause_chars)
        messages = [{"role": "system", "content": build_system_prompt(self.persona)}] + self.history[
            -self.settings.max_history_messages :
        ]
        await self._set_state("thinking")
        await self.send_json({"type": "assistant.turn_start", "turn_id": turn_id})

        async def produce_text() -> None:
            full_text = ""
            first_token = True
            async for token in self.llm.stream(messages):
                clean_token = token.replace("\u0000", "")
                if not clean_token:
                    continue
                if first_token:
                    first_token = False
                    turn.llm_first_token_ms = round((time.perf_counter() - turn.started_at) * 1000, 1)
                    await self.send_json(
                        {
                            "type": "metrics.llm_first_token",
                            "turn_id": turn_id,
                            "milliseconds": turn.llm_first_token_ms,
                        }
                    )
                full_text += clean_token
                await self.send_json({"type": "assistant.delta", "turn_id": turn_id, "text": clean_token})
                for clause in buffer.feed(clean_token):
                    await clause_queue.put(clause)
            for clause in buffer.flush():
                await clause_queue.put(clause)
            await clause_queue.put(None)
            await self.send_json(
                {"type": "assistant.text_done", "turn_id": turn_id, "text": sanitize_spoken_text(full_text)}
            )

        async def consume_audio() -> None:
            while True:
                clause = await clause_queue.get()
                if clause is None:
                    return
                await self._speak_clause(turn, clause)

        producer = asyncio.create_task(produce_text(), name=f"llm-producer-{turn_id[:8]}")
        consumer = asyncio.create_task(consume_audio(), name=f"tts-consumer-{turn_id[:8]}")
        try:
            await asyncio.gather(producer, consumer)
            await self.send_json({"type": "assistant.generation_done", "turn_id": turn_id})
            if not turn.streams:
                await self._set_state("idle")
        except asyncio.CancelledError:
            producer.cancel()
            consumer.cancel()
            await asyncio.gather(producer, consumer, return_exceptions=True)
            raise
        except Exception as exc:
            producer.cancel()
            consumer.cancel()
            await asyncio.gather(producer, consumer, return_exceptions=True)
            await self._send_error("assistant", exc)
            await self.send_json({"type": "audio.stop", "channel": CHANNEL_MAIN, "turn_id": turn_id})
            self._assistant_playing = False
            await self._set_state("idle")

    async def _speak_clause(self, turn: PendingAssistantTurn, clause: str) -> None:
        self._stream_seq += 1
        stream_id = self._stream_seq
        turn.streams.append(stream_id)
        turn.clauses[stream_id] = clause
        self._stream_to_turn[stream_id] = turn.turn_id
        request_id = f"{turn.turn_id}:{stream_id}"
        sent_start = False

        async for chunk in self.tts.stream(
            profile_id=self.voice_profile_id,
            text=clause,
            language=self.settings.tts_language,
            chunk_size=self.settings.tts_chunk_size,
            request_id=request_id,
        ):
            if not sent_start:
                sent_start = True
                self._assistant_playing = True
                if turn.first_audio_ms is None:
                    turn.first_audio_ms = round((time.perf_counter() - turn.started_at) * 1000, 1)
                    await self.send_json(
                        {
                            "type": "metrics.first_audio",
                            "turn_id": turn.turn_id,
                            "milliseconds": turn.first_audio_ms,
                            "provider_timing": chunk.timing,
                        }
                    )
                await self._set_state("speaking")
                await self.send_json(
                    {
                        "type": "audio.begin",
                        "turn_id": turn.turn_id,
                        "stream_id": stream_id,
                        "sample_rate": chunk.sample_rate,
                        "channel": CHANNEL_MAIN,
                        "text": clause,
                    }
                )
            await self.send_bytes(pack_audio_frame(CHANNEL_MAIN, stream_id, chunk.sample_rate, chunk.pcm16))
        if sent_start:
            await self.send_json({"type": "audio.end", "stream_id": stream_id, "channel": CHANNEL_MAIN})

    async def _backchannel_loop(self) -> None:
        try:
            await asyncio.sleep(self.settings.backchannel_after_ms / 1000)
            while self._user_speaking:
                now = time.monotonic()
                if now - self._last_backchannel_at >= self.settings.backchannel_cooldown_ms / 1000:
                    self._last_backchannel_at = now
                    text = random.choice(self.persona.backchannels or ["응."])
                    await self._play_backchannel(text)
                await asyncio.sleep(self.settings.backchannel_cooldown_ms / 1000)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.send_json({"type": "warning", "message": f"추임새 생성 실패: {exc}"})

    async def _play_backchannel(self, text: str) -> None:
        if not self.voice_profile_id and self.settings.tts_mode != "mock":
            return
        self._stream_seq += 1
        stream_id = self._stream_seq
        sent_start = False
        async for chunk in self.tts.stream(
            profile_id=self.voice_profile_id,
            text=text,
            language=self.settings.tts_language,
            chunk_size=max(2, self.settings.tts_chunk_size),
            request_id=f"backchannel:{stream_id}",
        ):
            if not sent_start:
                sent_start = True
                await self.send_json(
                    {
                        "type": "audio.begin",
                        "stream_id": stream_id,
                        "sample_rate": chunk.sample_rate,
                        "channel": CHANNEL_BACKCHANNEL,
                        "text": text,
                    }
                )
            await self.send_bytes(pack_audio_frame(CHANNEL_BACKCHANNEL, stream_id, chunk.sample_rate, chunk.pcm16))
        if sent_start:
            await self.send_json({"type": "audio.end", "stream_id": stream_id, "channel": CHANNEL_BACKCHANNEL})

    async def _audio_played(self, stream_id: int) -> None:
        turn_id = self._stream_to_turn.get(stream_id)
        if not turn_id:
            return
        turn = self._turns.get(turn_id)
        if turn:
            turn.played.add(stream_id)

    async def _playback_idle(self, requested_turn_id: str = "") -> None:
        self._assistant_playing = False
        await self.send_json({"type": "audio.unduck", "release_ms": 40})
        turn_id = requested_turn_id or self._active_turn_id
        if turn_id and turn_id in self._turns:
            turn = self._turns[turn_id]
            spoken = turn.played_text()
            if spoken:
                self.history.append({"role": "assistant", "content": spoken})
                self.history = self.history[-self.settings.max_history_messages :]
            await self.send_json(
                {
                    "type": "assistant.committed",
                    "turn_id": turn_id,
                    "spoken_text": spoken,
                    "interrupted": turn.interrupted,
                }
            )
            for sid in turn.streams:
                self._stream_to_turn.pop(sid, None)
            self._turns.pop(turn_id, None)
            if self._active_turn_id == turn_id:
                self._active_turn_id = None
        if not self._user_speaking and not self._assistant_active():
            await self._set_state("idle")

    async def _stt_events_loop(self) -> None:
        try:
            while True:
                event = await self.stt.events.get()
                event_type = event.get("type")
                if event_type == "partial":
                    self._latest_partial = str(event.get("text", "")).strip()
                    await self.send_json(
                        {
                            "type": "transcript.partial",
                            "text": self._latest_partial,
                            "language": event.get("language"),
                        }
                    )
                elif event_type == "error":
                    await self.send_json(event)
                elif event_type == "ready":
                    await self.send_json({"type": "stt.ready", **{k: v for k, v in event.items() if k != "type"}})
        except asyncio.CancelledError:
            raise

    async def _set_state(self, state: str) -> None:
        await self.send_json(
            {
                "type": "session.state",
                "state": state,
                "user_speaking": self._user_speaking,
                "assistant_playing": self._assistant_playing,
            }
        )

    async def _send_error(self, source: str, exc: Exception) -> None:
        await self.send_json({"type": "error", "source": source, "message": f"{type(exc).__name__}: {exc}"})

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        async with self._send_lock:
            await self.websocket.send_json(payload)

    async def send_bytes(self, payload: bytes) -> None:
        if self._closed:
            return
        async with self._send_lock:
            await self.websocket.send_bytes(payload)

    @staticmethod
    async def _cancel_task(task: asyncio.Task | None) -> None:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        tasks = [
            self._stt_events_task,
            self._assistant_task,
            self._barge_task,
            self._backchannel_task,
            *self._finalize_tasks,
        ]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        await self.stt.close()
