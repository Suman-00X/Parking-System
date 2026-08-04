"""Splits clinical prose into sentences, which is how dictation is matched to template fields.

Order: split a passage (split_sentences), or walk it keeping each sentence's position in the
original text (iter_sentences_with_offsets).
"""

from __future__ import annotations

import re

#: Abbreviations that end in a period mid-sentence.
_ABBREVIATIONS: frozenset[str] = frozenset({"dr", "mr", "mrs", "ms", "prof", "no", "approx", "vs", "cf", "e.g", "i.e", "st"})

#: A boundary is `;`, `?`, `!`, a newline, or a `.` that is **not** between two
#: digits. The lookarounds are what keep "3.2" intact.
_BOUNDARY = re.compile(r"(?<!\d)\.(?!\d)|[;?!]|\n+")


def split_sentences(text: str) -> list[str]:
    """Split clinical prose into sentences, keeping decimals intact."""
    if not text:
        return []

    pieces: list[str] = []
    start = 0
    for match in _BOUNDARY.finditer(text):
        piece = text[start : match.start()]
        if _ends_with_abbreviation(piece):
            # Not a boundary after all — let the sentence continue.
            continue
        if piece.strip():
            pieces.append(piece.strip())
        start = match.end()

    tail = text[start:]
    if tail.strip():
        pieces.append(tail.strip())
    return pieces


def _ends_with_abbreviation(piece: str) -> bool:
    tail = re.split(r"[\s(]", piece.strip())[-1].lower() if piece.strip() else ""
    return tail in _ABBREVIATIONS


def iter_sentences_with_offsets(text: str) -> list[tuple[int, int, str]]:
    """`(start, end, sentence)` — for callers that must cite character ranges."""
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    for sentence in split_sentences(text):
        index = text.find(sentence, cursor)
        if index < 0:  # pragma: no cover - defensive
            continue
        spans.append((index, index + len(sentence), sentence))
        cursor = index + len(sentence)
    return spans
