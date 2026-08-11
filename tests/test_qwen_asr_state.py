from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from services.qwen_asr import server


class InPlaceModel:
    def __init__(self) -> None:
        self.steps = 0
        self.finished = False

    def streaming_transcribe(self, pcm, state):
        assert isinstance(pcm, np.ndarray)
        self.steps += 1
        state.language = "Korean"
        state.text = f"부분 {self.steps}"
        # Official Qwen3-ASR examples use this API for its side effect. Returning
        # None catches accidental `state = model.streaming_transcribe(...)` code.
        return None

    def finish_streaming_transcribe(self, state):
        self.finished = True
        state.text = "최종 문장"
        return None


def test_streaming_state_is_mutated_in_place(monkeypatch) -> None:
    model = InPlaceModel()
    state = SimpleNamespace(language="", text="")
    monkeypatch.setattr(server, "_model", model)

    returned, language, text = server._decode_step(state, np.zeros(1600, dtype=np.float32))
    assert returned is state
    assert language == "Korean"
    assert text == "부분 1"

    returned, language, text = server._finish(state, np.zeros(800, dtype=np.float32))
    assert returned is state
    assert model.finished
    assert language == "Korean"
    assert text == "최종 문장"
