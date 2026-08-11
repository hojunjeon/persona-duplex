import pytest

from gateway.app.protocol import CHANNEL_MAIN, pack_audio_frame, unpack_audio_frame


def test_audio_frame_roundtrip() -> None:
    payload = pack_audio_frame(CHANNEL_MAIN, 7, 24000, b"\x00\x01\x02\x03")
    frame = unpack_audio_frame(payload)
    assert frame.channel == CHANNEL_MAIN
    assert frame.stream_id == 7
    assert frame.sample_rate == 24000
    assert frame.pcm16 == b"\x00\x01\x02\x03"


def test_reject_odd_pcm() -> None:
    with pytest.raises(ValueError):
        pack_audio_frame(CHANNEL_MAIN, 1, 24000, b"\x00")
