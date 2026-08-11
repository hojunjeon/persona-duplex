from gateway.app.providers import (
    DeepgramRealtimeSTT,
    ElevenLabsRealtimeSTT,
    MockLLM,
    MockSTT,
    MockTTS,
    QwenSTTWebSocket,
    QwenTTSWebSocket,
    SonioxRealtimeSTT,
    create_llm,
    create_stt,
    create_tts,
)


def kwargs() -> dict:
    return {
        "url": "ws://localhost:9999/ws",
        "language": "Korean",
        "mock_transcript": "테스트",
        "api_key": "not-a-real-key",
        "cloud_model": "test-model",
        "cloud_language_code": "ko",
    }


def test_stt_factory_modes() -> None:
    assert isinstance(create_stt("mock", **kwargs()), MockSTT)
    assert isinstance(create_stt("qwen_ws", **kwargs()), QwenSTTWebSocket)
    assert isinstance(create_stt("elevenlabs_ws", **kwargs()), ElevenLabsRealtimeSTT)
    assert isinstance(create_stt("soniox_ws", **kwargs()), SonioxRealtimeSTT)
    assert isinstance(create_stt("deepgram_ws", **kwargs()), DeepgramRealtimeSTT)


def test_other_provider_factories() -> None:
    assert isinstance(
        create_llm(
            "mock",
            base_url="http://localhost/v1",
            api_key="x",
            model="x",
            temperature=0.1,
            max_tokens=10,
            timeout_seconds=2,
        ),
        MockLLM,
    )
    assert isinstance(create_tts("mock", url="ws://localhost"), MockTTS)
    assert isinstance(create_tts("qwen_ws", url="ws://localhost"), QwenTTSWebSocket)
