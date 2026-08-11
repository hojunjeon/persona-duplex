from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import AsyncIterator

from gateway.app.config import Settings
from gateway.app.providers import StreamingLLM, StreamingSTT, StreamingTTS, TTSChunk
from gateway.app.session import VoiceSession


ROOT = Path(__file__).resolve().parents[1]


class FakeWebSocket:
    def __init__(self) -> None:
        self.json_events: list[dict] = []
        self.binary_events: list[bytes] = []

    async def send_json(self, payload: dict) -> None:
        self.json_events.append(payload)

    async def send_bytes(self, payload: bytes) -> None:
        self.binary_events.append(payload)


class BlockingSTT(StreamingSTT):
    def __init__(self) -> None:
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.buffer = bytearray()
        self.reset_count = 0
        self.finish_count = 0
        self.finished_audio: list[bytes] = []
        self.first_finish_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def start(self) -> None:
        return None

    async def send_audio(self, pcm16: bytes) -> None:
        self.buffer.extend(pcm16)

    async def reset(self) -> None:
        self.reset_count += 1
        self.buffer.clear()

    async def finish(self, utterance_id: str) -> str:
        del utterance_id
        self.finish_count += 1
        index = self.finish_count
        self.finished_audio.append(bytes(self.buffer))
        if index == 1:
            self.first_finish_started.set()
            await self.release_first.wait()
        self.buffer.clear()
        return "첫 번째 발화" if index == 1 else "두 번째 발화"

    async def close(self) -> None:
        return None


class CountingLLM(StreamingLLM):
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        del messages
        self.calls += 1
        if False:
            yield ""


class EmptyTTS(StreamingTTS):
    async def stream(
        self,
        *,
        profile_id: str,
        text: str,
        language: str,
        chunk_size: int,
        request_id: str,
    ) -> AsyncIterator[TTSChunk]:
        del profile_id, text, language, chunk_size, request_id
        if False:
            yield TTSChunk(b"", 24000, {})


def make_settings() -> Settings:
    return replace(
        Settings(),
        persona_dir=ROOT / "gateway" / "personas",
        backchannel_enabled=False,
        tts_mode="mock",
        pre_roll_ms=20,
    )


def test_consecutive_utterances_are_serialized_without_stale_reply() -> None:
    async def scenario() -> None:
        ws = FakeWebSocket()
        stt = BlockingSTT()
        llm = CountingLLM()
        session = VoiceSession(ws, settings=make_settings(), stt=stt, llm=llm, tts=EmptyTTS())

        first_packet = (b"\x01\x00") * 320
        second_packet = (b"\x02\x00") * 320

        await session._speech_start()
        await session.handle_audio(first_packet)
        await session._speech_end()
        await asyncio.wait_for(stt.first_finish_started.wait(), timeout=1)

        # Begin and end a second utterance while the first STT commit is blocked.
        await session._speech_start()
        await session.handle_audio(second_packet)
        await session._speech_end()
        pending = list(session._finalize_tasks)
        assert len(pending) == 2

        stt.release_first.set()
        await asyncio.wait_for(asyncio.gather(*pending), timeout=2)
        await asyncio.sleep(0.02)

        assert stt.finish_count == 2
        assert stt.finished_audio[0].endswith(first_packet)
        assert stt.finished_audio[1].endswith(second_packet)
        assert stt.reset_count == 2  # first live turn + second deferred replay
        assert [item["content"] for item in session.history if item["role"] == "user"] == [
            "첫 번째 발화",
            "두 번째 발화",
        ]
        assert llm.calls == 1
        assert any(event.get("type") == "assistant.deferred" for event in ws.json_events)

        await session.close()

    asyncio.run(scenario())


def test_deferred_short_ack_does_not_reset_pending_stt() -> None:
    async def scenario() -> None:
        ws = FakeWebSocket()
        stt = BlockingSTT()
        session = VoiceSession(
            ws,
            settings=make_settings(),
            stt=stt,
            llm=CountingLLM(),
            tts=EmptyTTS(),
        )

        # Simulate an earlier commit holding the provider while the assistant is active.
        session._pending_finalizations = 1
        session._assistant_playing = True
        await session._speech_start()
        await session.handle_audio((b"\x03\x00") * 160)
        session._latest_partial = "응"
        resets_before = stt.reset_count
        await session._speech_end()

        assert stt.reset_count == resets_before
        assert session._buffer_current_stt is False
        assert session._current_stt_audio == bytearray()
        assert any(event.get("type") == "user.backchannel" for event in ws.json_events)

        session._pending_finalizations = 0
        await session.close()

    asyncio.run(scenario())
