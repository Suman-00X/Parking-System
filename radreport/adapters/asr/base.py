"""The interface every speech engine implements, so engines can be swapped without touching the pipeline.

Defines: ASREngine, what it is given (ASRConfig) and what it returns (ASRResult, Word).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from radreport.core.hashing import hash_config


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    start_ms: int
    end_ms: int
    confidence: float | None = None
    speaker_label: str | None = None
    alternatives: tuple[tuple[str, float], ...] = ()
    """N-best at this position."""


@dataclass(slots=True)
class ASRResult:
    text: str
    words: list[Word]
    overall_confidence: float | None = None
    language: str | None = None
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    """Full vendor payload, retained. Reprocessing an archive is cheaper than re-transcribing it."""


@dataclass(frozen=True, slots=True)
class ASRConfig:
    """Everything that changes the output. Hashed into `asr_run.config_hash`."""

    keyterms: tuple[str, ...] = ()
    """Biasing list from the active lexicon set. The CODEWORD_RECALL is the metric this moves."""

    language: str = "en-IN"
    diarize: bool = False
    punctuate: bool = True
    model_variant: str | None = None
    extra: tuple[tuple[str, str], ...] = ()

    def config_hash(self) -> str:
        return hash_config({"keyterms": sorted(self.keyterms), "language": self.language, "diarize": self.diarize, "punctuate": self.punctuate, "model_variant": self.model_variant, "extra": sorted(self.extra)})


class ASREngine(Protocol):
    """One engine. `engine`/`engine_version` land on `asr_run` and are pinned."""

    engine: str
    engine_version: str

    async def transcribe(self, audio: bytes, config: ASRConfig) -> ASRResult: ...

    def supports_keyterm_biasing(self) -> bool: ...
