from __future__ import annotations

import asyncio

from gateway.app.providers import ElevenLabsRealtimeSTT


def test_elevenlabs_retains_nonempty_commit_tail() -> None:
    async def scenario() -> None:
        stt = ElevenLabsRealtimeSTT(api_key="test", model="scribe_v2_realtime", language_code="ko")
        sent: list[tuple[bytes, bool]] = []

        async def fake_send(payload: bytes, *, commit: bool) -> None:
            sent.append((payload, commit))

        stt._ws = object()  # type: ignore[assignment]
        stt._send_audio_event = fake_send  # type: ignore[method-assign]
        await stt.send_audio(b"\x01\x00" * 3200)  # 200 ms
        assert sent == [(b"\x01\x00" * 1600, False)]
        assert len(stt._audio_buffer) == 3200

    asyncio.run(scenario())
