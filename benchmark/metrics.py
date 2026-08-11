from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence


def normalize_korean(text: str, *, keep_spaces: bool = True) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^0-9a-z가-힣\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if keep_spaces else text.replace(" ", "")


def edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    previous = list(range(len(hypothesis) + 1))
    for i, ref_item in enumerate(reference, 1):
        current = [i]
        for j, hyp_item in enumerate(hypothesis, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[j] + 1,
                    previous[j - 1] + (ref_item != hyp_item),
                )
            )
        previous = current
    return previous[-1]


def cer(reference: str, hypothesis: str) -> float:
    ref = list(normalize_korean(reference, keep_spaces=False))
    hyp = list(normalize_korean(hypothesis, keep_spaces=False))
    return edit_distance(ref, hyp) / max(1, len(ref))


def wer(reference: str, hypothesis: str) -> float:
    ref = normalize_korean(reference).split()
    hyp = normalize_korean(hypothesis).split()
    return edit_distance(ref, hyp) / max(1, len(ref))
