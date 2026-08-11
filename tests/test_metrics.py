from benchmark.metrics import cer, normalize_korean, wer


def test_korean_normalization() -> None:
    assert normalize_korean("안녕,  WORLD!") == "안녕 world"


def test_cer_and_wer() -> None:
    assert cer("안녕하세요", "안녕하세요") == 0
    assert wer("나는 학교에 간다", "나는 학교에 간다") == 0
    assert cer("가나다", "가나") == 1 / 3
    assert wer("가 나 다", "가 나") == 1 / 3
