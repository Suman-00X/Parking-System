"""Maps a position in the transcript text back to the moment in the audio where it was said.

Order: build the map once per transcript (build_timing_map) -> look up the audio span for a
piece of text (audio_span) -> check how much of it is covered (coverage).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from radreport.adapters.asr.base import Word
from radreport.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WordTiming:
    """One word's position in the transcript and in the audio."""

    char_start: int
    char_end: int
    start_ms: int
    end_ms: int
    is_interpolated: bool = False


def build_timing_map(text: str, words: list[Word]) -> list[WordTiming]:
    """Locate each ASR word in the transcript, preserving its timing."""
    timings: list[WordTiming] = []
    cursor = 0
    lowered = text.lower()

    for word in words:
        token = word.text.strip().lower()
        if not token:
            continue
        index = lowered.find(token, cursor)
        if index < 0:
            # The word is not in the transcript — normal after post-correction or ROVER.
            continue
        timings.append(WordTiming(char_start=index, char_end=index + len(token), start_ms=word.start_ms, end_ms=word.end_ms))
        cursor = index + len(token)

    if words and not timings:
        log.warning("timing_map_empty", words=len(words), detail="no ASR word matched the transcript; audio spans will be absent")
    return timings


def audio_span(timings: list[WordTiming], char_start: int, char_end: int) -> tuple[int, int, bool]:
    """`(start_ms, end_ms, is_interpolated)` for a character range."""
    if not timings or char_end <= char_start:
        return 0, 0, True

    starts = [t.char_start for t in timings]

    # Words wholly or partly inside the range.
    inside = [t for t in timings if t.char_end > char_start and t.char_start < char_end]
    if inside:
        return (min(t.start_ms for t in inside), max(t.end_ms for t in inside), any(t.is_interpolated for t in inside))

    # Nothing matched — the range covers text no ASR word was located in
    # (post-corrected or voted). Interpolate between the neighbours.
    position = bisect.bisect_left(starts, char_start)
    before = timings[position - 1] if position > 0 else None
    after = timings[position] if position < len(timings) else None

    if before and after:
        return before.end_ms, after.start_ms, True
    if before:
        return before.end_ms, before.end_ms, True
    if after:
        return after.start_ms, after.start_ms, True
    return 0, 0, True


def coverage(timings: list[WordTiming], text: str) -> float:
    """Share of the transcript's characters a timing was located for."""
    if not text:
        return 0.0
    covered = sum(t.char_end - t.char_start for t in timings)
    return round(min(1.0, covered / len(text)), 4)
