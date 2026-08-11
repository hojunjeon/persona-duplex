from gateway.app.text import ClauseBuffer, is_short_backchannel, sanitize_spoken_text


def test_sanitize_spoken_text() -> None:
    text = "**핵심**은 [문서](https://example.com)와 `코드`야."
    assert sanitize_spoken_text(text) == "핵심은 문서와 코드야."


def test_clause_buffer_streaming() -> None:
    buffer = ClauseBuffer(max_chars=24, min_chars=5)
    assert buffer.feed("첫 문장은 짧아") == []
    assert buffer.feed(". 두 번째 문장도 이어져!") == ["첫 문장은 짧아.", "두 번째 문장도 이어져!"]
    assert buffer.flush() == []


def test_backchannel_classifier_is_conservative() -> None:
    assert is_short_backchannel("응응")
    assert is_short_backchannel("그렇구나.")
    assert not is_short_backchannel("잠깐만 멈춰")
    assert not is_short_backchannel("")
