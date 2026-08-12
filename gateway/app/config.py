from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("GATEWAY_HOST", "0.0.0.0")
    port: int = int(os.getenv("GATEWAY_PORT", "8080"))
    app_name: str = "Persona Duplex"

    # STT: qwen_ws, elevenlabs_ws, soniox_ws, deepgram_ws
    stt_mode: str = os.getenv("STT_MODE", "qwen_ws")
    stt_ws_url: str = os.getenv("STT_WS_URL", "ws://qwen-asr:8101/ws")
    stt_http_url: str = os.getenv("STT_HTTP_URL", "http://qwen-asr:8101")
    stt_language: str = os.getenv("STT_LANGUAGE", "Korean")
    stt_api_key: str = os.getenv("STT_API_KEY", "")
    stt_cloud_model: str = os.getenv("STT_CLOUD_MODEL", "scribe_v2_realtime")
    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "")
    soniox_api_key: str = os.getenv("SONIOX_API_KEY", "")
    deepgram_api_key: str = os.getenv("DEEPGRAM_API_KEY", "")
    stt_cloud_language_code: str = os.getenv("STT_CLOUD_LANGUAGE_CODE", "ko")

    # TTS: qwen_ws
    tts_mode: str = os.getenv("TTS_MODE", "qwen_ws")
    tts_ws_url: str = os.getenv("TTS_WS_URL", "ws://qwen-tts:8102/ws/synthesize")
    tts_http_url: str = os.getenv("TTS_HTTP_URL", "http://qwen-tts:8102")
    tts_language: str = os.getenv("TTS_LANGUAGE", "Korean")
    tts_chunk_size: int = int(os.getenv("TTS_CHUNK_SIZE", "4"))

    # Any OpenAI-compatible chat-completions endpoint works. Ollama is the default.
    llm_mode: str = os.getenv("LLM_MODE", "openai_compatible")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "http://host.docker.internal:11434/v1")
    llm_api_key: str = os.getenv("LLM_API_KEY", "ollama")
    llm_model: str = os.getenv("LLM_MODEL", "qwen3:8b")
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.75"))
    llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "260"))
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))

    persona_dir: Path = Path(os.getenv("PERSONA_DIR", "/app/personas"))
    # Built-in personas live in the image and remain immutable. User-created
    # personas are persisted in the separately mounted data directory.
    persona_data_dir: Path = Path(os.getenv("PERSONA_DATA_DIR", "/data/personas"))
    default_persona: str = os.getenv("DEFAULT_PERSONA", "default")
    benchmark_dir: Path = Path(os.getenv("BENCHMARK_DIR", "/data/benchmark"))

    # Duplex timing. The browser ducks immediately, then this timer decides whether
    # speech is a tiny acknowledgement or a real interruption.
    barge_in_confirm_ms: int = int(os.getenv("BARGE_IN_CONFIRM_MS", "420"))
    short_backchannel_max_ms: int = int(os.getenv("SHORT_BACKCHANNEL_MAX_MS", "850"))
    empty_backchannel_max_ms: int = int(os.getenv("EMPTY_BACKCHANNEL_MAX_MS", "320"))
    backchannel_after_ms: int = int(os.getenv("BACKCHANNEL_AFTER_MS", "1900"))
    backchannel_cooldown_ms: int = int(os.getenv("BACKCHANNEL_COOLDOWN_MS", "3300"))
    backchannel_enabled: bool = _env_bool("BACKCHANNEL_ENABLED", True)
    pre_roll_ms: int = int(os.getenv("PRE_ROLL_MS", "440"))
    vad_start_frames: int = int(os.getenv("VAD_START_FRAMES", "3"))
    vad_end_ms_assistant: int = int(os.getenv("VAD_END_MS_ASSISTANT", "300"))
    vad_end_ms_idle: int = int(os.getenv("VAD_END_MS_IDLE", "520"))
    vad_threshold_min: float = float(os.getenv("VAD_THRESHOLD_MIN", "0.012"))
    vad_noise_multiplier_assistant: float = float(os.getenv("VAD_NOISE_MULTIPLIER_ASSISTANT", "3.4"))
    vad_noise_multiplier_idle: float = float(os.getenv("VAD_NOISE_MULTIPLIER_IDLE", "2.7"))
    max_history_messages: int = int(os.getenv("MAX_HISTORY_MESSAGES", "18"))
    max_clause_chars: int = int(os.getenv("MAX_CLAUSE_CHARS", "54"))
    min_clause_chars: int = int(os.getenv("MIN_CLAUSE_CHARS", "8"))

    client_origin: str = os.getenv("CLIENT_ORIGIN", "*")
    allow_remote_microphone: bool = _env_bool("ALLOW_REMOTE_MICROPHONE", False)


settings = Settings()
