from __future__ import annotations

import csv
from pathlib import Path

from benchmark.apply_selection import merge_env, read_values
from benchmark.select_best import choose, summarize


FIELDS = [
    "provider", "audio_path", "reference", "transcript", "audio_seconds",
    "first_partial_ms", "final_after_end_ms", "total_ms", "rtf", "cer", "wer", "error",
]


def write_results(path: Path) -> None:
    rows = [
        ["qwen", "a.wav", "안녕", "안녕", 1, 420, 500, 1500, 1.5, 0.02, 0.0, ""],
        ["qwen", "b.wav", "테스트", "테스트", 1, 450, 560, 1600, 1.6, 0.04, 0.0, ""],
        ["elevenlabs", "a.wav", "안녕", "안영", 1, 180, 280, 1300, 1.3, 0.08, 1.0, ""],
        ["elevenlabs", "b.wav", "테스트", "테스트", 1, 190, 300, 1320, 1.32, 0.03, 0.0, ""],
        ["deepgram", "a.wav", "안녕", "", 1, "", "", "", "", "", "", "timeout"],
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        writer.writerows(rows)


def test_summarize_and_choose(tmp_path: Path) -> None:
    path = tmp_path / "results.csv"
    write_results(path)
    summaries = summarize(path)
    assert {item.provider for item in summaries} == {"qwen", "elevenlabs"}
    assert choose(summaries, "accuracy", 1200).provider == "qwen"
    assert choose(summaries, "latency", 1200).provider == "elevenlabs"


def test_env_merge(tmp_path: Path) -> None:
    selection = tmp_path / "selected.env"
    selection.write_text("STT_MODE=qwen_ws\nASR_MODEL_ID=Qwen/Qwen3-ASR-1.7B\n", encoding="utf-8")
    target = tmp_path / ".env"
    target.write_text("STT_MODE=mock\nLLM_MODEL=qwen3:8b\n", encoding="utf-8")
    values = read_values(selection)
    merge_env(target, values)
    content = target.read_text(encoding="utf-8")
    assert "STT_MODE=qwen_ws" in content
    assert "ASR_MODEL_ID=Qwen/Qwen3-ASR-1.7B" in content
    assert "LLM_MODEL=qwen3:8b" in content
