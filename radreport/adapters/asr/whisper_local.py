"""A locally hosted Whisper engine, plus a stub used in tests.

Defines: WhisperLocalEngine, the default engine, and StubASREngine for tests that need no real
audio.
"""

from __future__ import annotations

import time
from typing import Any

from radreport.adapters.asr.base import ASRConfig, ASRResult, Word
from radreport.core.errors import ProviderError
from radreport.core.logging import get_logger

log = get_logger(__name__)


class WhisperLocalEngine:
    """faster-whisper behind the common interface."""

    def __init__(self, *, model_size: str = "medium", device: str = "auto", compute_type: str = "int8", model: Any | None = None) -> None:
        self.engine = "whisper_local"
        self.engine_version = f"faster-whisper/{model_size}"
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model = model

    def supports_keyterm_biasing(self) -> bool:
        """Whisper has no true keyterm API — `initial_prompt` is a weak proxy."""
        return False

    def _load(self) -> Any:
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:  # pragma: no cover
                raise ProviderError("faster-whisper is not installed; install it or point the ASR adapter at a hosted engine") from exc
            self._model = WhisperModel(self._model_size, device=self._device, compute_type=self._compute_type)
        return self._model

    async def transcribe(self, audio: bytes, config: ASRConfig) -> ASRResult:
        import asyncio
        import io

        started = time.perf_counter_ns()
        model = self._load()

        def _run() -> tuple[list[Any], Any]:
            segments, info = model.transcribe(
                io.BytesIO(audio),
                language=config.language.split("-")[0],
                word_timestamps=True,
                # Bias via initial_prompt — the closest Whisper gets. Truncated
                # because an over-long prompt degrades rather than helps.
                initial_prompt=", ".join(config.keyterms[:100]) or None,
                vad_filter=True,
            )
            return list(segments), info

        segments, info = await asyncio.to_thread(_run)

        words: list[Word] = []
        parts: list[str] = []
        for segment in segments:
            parts.append(segment.text)
            for word in getattr(segment, "words", None) or []:
                words.append(Word(text=word.word.strip(), start_ms=int(word.start * 1000), end_ms=int(word.end * 1000), confidence=getattr(word, "probability", None)))

        return ASRResult(text="".join(parts).strip(), words=words, overall_confidence=None, language=getattr(info, "language", config.language), latency_ms=(time.perf_counter_ns() - started) // 1_000_000, raw={"duration": getattr(info, "duration", None)})


def spoken_text_in(audio: bytes) -> str | None:
    """The `ICMT` comment of a WAV file's INFO list, where synthetic demo dictations carry what they "say"."""
    if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return None
    offset = 12
    while offset + 8 <= len(audio):
        chunk, size = audio[offset : offset + 4], int.from_bytes(audio[offset + 4 : offset + 8], "little")
        body = audio[offset + 8 : offset + 8 + size]
        if chunk == b"LIST" and body[:4] == b"INFO":
            inner = 4
            while inner + 8 <= len(body):
                sub, sub_size = body[inner : inner + 4], int.from_bytes(body[inner + 4 : inner + 8], "little")
                if sub == b"ICMT":
                    return body[inner + 8 : inner + 8 + sub_size].rstrip(b"\x00").decode("utf-8", errors="replace").strip() or None
                inner += 8 + sub_size + (sub_size & 1)
        offset += 8 + size + (size & 1)
    return None


class StubASREngine:
    """Deterministic fake for tests and local development: the text a synthetic WAV carries, else a fixed sentence."""

    def __init__(self, *, text: str = "study type ultrasound abdomen routine") -> None:
        self.engine = "stub"
        self.engine_version = "v0"
        self._text = text

    def supports_keyterm_biasing(self) -> bool:
        return True

    async def transcribe(self, audio: bytes, config: ASRConfig) -> ASRResult:
        text = spoken_text_in(audio) or self._text
        tokens = text.split()
        step = 400
        words = [Word(text=token, start_ms=i * step, end_ms=(i + 1) * step, confidence=0.95) for i, token in enumerate(tokens)]
        return ASRResult(text=text, words=words, overall_confidence=0.95, language=config.language, latency_ms=1)
