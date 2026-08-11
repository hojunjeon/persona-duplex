from gateway.app.providers import IncrementalThinkFilter


def test_think_filter_handles_split_tags() -> None:
    f = IncrementalThinkFilter()
    chunks = ["안녕 <thi", "nk>비공개 추론", " 중</th", "ink> 반가워"]
    out: list[str] = []
    for chunk in chunks:
        out.extend(f.feed(chunk))
    out.extend(f.flush())
    assert "".join(out) == "안녕  반가워"
    assert "비공개" not in "".join(out)
