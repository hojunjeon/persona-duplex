from __future__ import annotations

import re

_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_BLOCK_MARKS = re.compile(r"(^|\s)[#>]+")
_EMPHASIS_MARKS = re.compile(r"[*_~]{1,3}")
_SPACE = re.compile(r"\s+")
_BACKCHANNEL_NORMALIZE = re.compile(r"[^0-9a-zA-Z가-힣]+")
_BACKCHANNELS = {
    "응", "어", "음", "아", "아하", "그래", "그렇구나", "그렇네", "맞아", "맞네",
    "네", "예", "오케이", "알겠어", "알겠습니다", "확인", "확인했어", "계속", "계속말해",
    "듣고있어", "듣는중", "좋아", "오", "흠", "으응", "응응", "그래그래",
}


def sanitize_spoken_text(text: str) -> str:
    """Turn streamed LLM prose into something a TTS engine can say without Markdown debris."""
    text = _CODE_FENCE.sub(" 코드 내용은 화면에 표시했어. ", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _URL.sub(" 링크 ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _BLOCK_MARKS.sub(r"\1", text)
    text = _EMPHASIS_MARKS.sub("", text)
    text = text.replace("\u200b", " ")
    return _SPACE.sub(" ", text).strip()


def is_short_backchannel(text: str) -> bool:
    """Conservative Korean acknowledgement classifier used only for barge-in timing."""
    normalized = _BACKCHANNEL_NORMALIZE.sub("", text.strip().lower())
    if not normalized:
        return False
    if normalized in _BACKCHANNELS:
        return True
    # Repeated acknowledgement syllables such as "응응응" or "네네".
    if len(normalized) <= 6 and (set(normalized) <= {"응"} or set(normalized) <= {"네"}):
        return True
    return False


class ClauseBuffer:
    """Incrementally emits short, speakable clauses from streamed LLM tokens."""

    def __init__(self, *, max_chars: int = 54, min_chars: int = 8) -> None:
        if min_chars < 1 or max_chars <= min_chars:
            raise ValueError("invalid clause length settings")
        self.max_chars = max_chars
        self.min_chars = min_chars
        self._buffer = ""

    @property
    def pending(self) -> str:
        return self._buffer

    def feed(self, token: str) -> list[str]:
        if not token:
            return []
        self._buffer += token
        emitted: list[str] = []

        while True:
            split_at = self._find_split()
            if split_at is None:
                break
            chunk = sanitize_spoken_text(self._buffer[:split_at])
            self._buffer = self._buffer[split_at:].lstrip()
            if chunk:
                emitted.append(chunk)
        return emitted

    def flush(self) -> list[str]:
        chunk = sanitize_spoken_text(self._buffer)
        self._buffer = ""
        return [chunk] if chunk else []

    def _find_split(self) -> int | None:
        if not self._buffer:
            return None

        # Prefer natural sentence endings. Newlines are hard boundaries.
        for index, char in enumerate(self._buffer):
            if char == "\n" and index + 1 >= self.min_chars:
                return index + 1
            if char in ".!?。！？…" and index + 1 >= self.min_chars:
                end = index + 1
                while end < len(self._buffer) and self._buffer[end] in ".!?。！？…\"'”’)]":
                    end += 1
                return end

        if len(self._buffer) < self.max_chars:
            return None

        # At the latency ceiling, cut at punctuation/space instead of waiting for
        # an LLM that has become emotionally attached to one giant sentence.
        window = self._buffer[: self.max_chars + 1]
        candidates = [window.rfind(mark) for mark in (",", "，", ";", ":", " ")]
        split = max(candidates)
        if split < self.min_chars:
            split = self.max_chars
        else:
            split += 1
        return split
