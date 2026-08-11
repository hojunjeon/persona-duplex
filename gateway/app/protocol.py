from __future__ import annotations

import struct
from dataclasses import dataclass

_HEADER = struct.Struct("<BII")
HEADER_SIZE = _HEADER.size
CHANNEL_MAIN = 1
CHANNEL_BACKCHANNEL = 2


@dataclass(frozen=True)
class AudioFrame:
    channel: int
    stream_id: int
    sample_rate: int
    pcm16: bytes


def pack_audio_frame(channel: int, stream_id: int, sample_rate: int, pcm16: bytes) -> bytes:
    if channel not in {CHANNEL_MAIN, CHANNEL_BACKCHANNEL}:
        raise ValueError(f"unsupported audio channel: {channel}")
    if stream_id < 0:
        raise ValueError("stream_id must be non-negative")
    if sample_rate < 8000 or sample_rate > 192000:
        raise ValueError("sample_rate is outside the supported range")
    if len(pcm16) % 2:
        raise ValueError("PCM16 payload must contain an even number of bytes")
    return _HEADER.pack(channel, stream_id, sample_rate) + pcm16


def unpack_audio_frame(payload: bytes) -> AudioFrame:
    if len(payload) < HEADER_SIZE:
        raise ValueError("audio frame is shorter than its header")
    channel, stream_id, sample_rate = _HEADER.unpack(payload[:HEADER_SIZE])
    return AudioFrame(channel, stream_id, sample_rate, payload[HEADER_SIZE:])
