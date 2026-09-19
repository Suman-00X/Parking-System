"""Compares speech engines on a lab's own audio and reports the numbers, deliberately refusing to collapse them into one score.

Order: run every engine over each partition of the gold set (run_bakeoff), scoring item by item
(ItemScore, has_non_latin_script) -> report per engine and per partition (EngineResult,
PartitionResult) -> render it (format_report).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.adapters.asr.base import ASRConfig, ASREngine
from radreport.adapters.storage.object_store import ObjectStore
from radreport.core.logging import get_logger
from radreport.core.types import CaptureDeviceClass
from radreport.db.models.evaluation import EvalItem, EvalSet
from radreport.db.models.ingestion import Recording
from radreport.eval.metrics.alignment import align, tokenize
from radreport.eval.metrics.asr_metrics import is_clinical_token

log = get_logger(__name__)

#: An engine whose insertions dominate its errors is inventing text, not
#: mishearing it — disqualifying regardless of headline WER.
INSERTION_SHARE_DISQUALIFIES = 0.40

#: Latin script only. A Devanagari or Tamil span in a reference transcript is
#: the code-switching signal asks for; the probe needs no language ID.
_NON_LATIN_RANGES: tuple[tuple[int, int], ...] = (
    (0x0900, 0x097F),  # Devanagari
    (0x0980, 0x09FF),  # Bengali
    (0x0B80, 0x0BFF),  # Tamil
    (0x0C00, 0x0C7F),  # Telugu
    (0x0C80, 0x0CFF),  # Kannada
    (0x0D00, 0x0D7F),  # Malayalam
    (0x0A00, 0x0A7F),  # Gurmukhi
)


def has_non_latin_script(text: str) -> bool:
    return any(any(low <= ord(ch) <= high for low, high in _NON_LATIN_RANGES) for ch in text)


@dataclass(frozen=True, slots=True)
class ItemScore:
    item_id: uuid.UUID
    recording_id: uuid.UUID
    capture_device_class: str
    quality_bucket: str
    radiologist_id: uuid.UUID | None
    wer: float
    insertion_rate: float
    insertion_share: float
    clinical_term_error_rate: float
    latency_ms: int
    code_switched_reference: bool


@dataclass(slots=True)
class PartitionResult:
    """One engine's numbers on one capture partition. Never merged across."""

    partition: str
    item_count: int = 0
    wer: float = 0.0
    insertion_rate: float = 0.0
    insertion_share_of_errors: float = 0.0
    clinical_term_error_rate: float = 0.0
    median_latency_ms: float = 0.0
    by_quality: dict[str, float] = field(default_factory=dict)
    by_speaker: dict[str, float] = field(default_factory=dict)

    @property
    def invents_text(self) -> bool:
        """The disqualifier, as a property rather than a footnote."""
        return self.insertion_share_of_errors >= INSERTION_SHARE_DISQUALIFIES


@dataclass(slots=True)
class EngineResult:
    engine: str
    engine_version: str
    config_hash: str
    partitions: dict[str, PartitionResult] = field(default_factory=dict)
    code_switching: dict[str, float] = field(default_factory=dict)
    """`{radiologist_id: WER on their code-switched items}`."""

    failures: list[tuple[uuid.UUID, str]] = field(default_factory=list)

    @property
    def current(self) -> PartitionResult | None:
        """The only partition a forward-looking decision may read."""
        return self.partitions.get("current")


@dataclass(slots=True)
class BakeoffReport:
    eval_set_id: uuid.UUID
    ran_at: dt.datetime
    engines: list[EngineResult] = field(default_factory=list)

    def rank(self) -> list[EngineResult]:
        """Best first, on the `current` partition, with insertions weighted."""

        def key(result: EngineResult) -> tuple[int, float, float]:
            current = result.current
            if current is None or current.item_count == 0:
                return (2, 1.0, 1.0)
            return (1 if current.invents_text else 0, current.clinical_term_error_rate, current.wer)

        return sorted(self.engines, key=key)

    def recommend(self) -> EngineResult | None:
        """The engine to adopt, or None if nothing is admissible."""
        ranked = self.rank()
        if not ranked:
            return None
        best = ranked[0]
        current = best.current
        if current is None or current.item_count == 0 or current.invents_text:
            return None
        return best


async def run_bakeoff(session: Session, *, eval_set: EvalSet, engines: list[ASREngine], store: ObjectStore, config: ASRConfig | None = None, concurrency: int = 4) -> BakeoffReport:
    """Run every engine over a frozen eval set and score them independently."""
    if not eval_set.is_frozen:
        raise ValueError(f"eval_set {eval_set.id} is not frozen; freeze it before the bake-off so every engine is scored against the identical reference")

    config = config or ASRConfig()
    items = _load_items(session, eval_set.id)
    if not items:
        raise ValueError(f"eval_set {eval_set.id} has no items")

    report = BakeoffReport(eval_set_id=eval_set.id, ran_at=dt.datetime.now(dt.UTC))
    for engine in engines:
        result = await _run_engine(engine, items, store, config, concurrency)
        report.engines.append(result)
        log.info("bakeoff_engine_complete", engine=engine.engine, engine_version=engine.engine_version, current_wer=result.current.wer if result.current else None, current_ins_rate=result.current.insertion_rate if result.current else None, invents_text=result.current.invents_text if result.current else None, failures=len(result.failures))
    return report


@dataclass(frozen=True, slots=True)
class _Item:
    item_id: uuid.UUID
    recording_id: uuid.UUID
    object_key: str
    reference: str
    capture_device_class: str
    quality_bucket: str
    radiologist_id: uuid.UUID | None


def _load_items(session: Session, eval_set_id: uuid.UUID) -> list[_Item]:
    rows = session.execute(select(EvalItem, Recording).join(Recording, Recording.id == EvalItem.recording_id).where(EvalItem.eval_set_id == eval_set_id)).all()
    return [_Item(item_id=item.id, recording_id=recording.id, object_key=recording.object_key, reference=item.gold_transcript_verbatim or "", capture_device_class=item.capture_device_class, quality_bucket=item.audio_quality_bucket or "unknown", radiologist_id=recording.radiologist_id) for item, recording in rows if item.gold_transcript_verbatim]


async def _run_engine(engine: ASREngine, items: list[_Item], store: ObjectStore, config: ASRConfig, concurrency: int) -> EngineResult:
    result = EngineResult(engine=engine.engine, engine_version=engine.engine_version, config_hash=config.config_hash())
    semaphore = asyncio.Semaphore(concurrency)

    async def transcribe(item: _Item) -> tuple[_Item, str | None, int, str | None]:
        async with semaphore:
            try:
                audio = store.get(item.object_key)
                asr = await engine.transcribe(audio, config)
                return item, asr.text, asr.latency_ms, None
            except Exception as exc:  # noqa: BLE001 - one bad item must not
                # abort a 250-item run; the failure is reported per item.
                return item, None, 0, f"{type(exc).__name__}: {exc}"

    scores: list[ItemScore] = []
    for item, hypothesis, latency_ms, error in await asyncio.gather(*(transcribe(item) for item in items)):
        if error is not None or hypothesis is None:
            result.failures.append((item.item_id, error or "no transcript returned"))
            continue
        scores.append(_score_item(item, hypothesis, latency_ms))

    result.partitions = _aggregate(scores)
    result.code_switching = _code_switching_breakdown(scores)
    return result


def _score_item(item: _Item, hypothesis: str, latency_ms: int) -> ItemScore:
    """All metrics from one alignment, per `metrics/alignment.py`."""
    reference_tokens = tokenize(item.reference)
    hypothesis_tokens = tokenize(hypothesis)
    alignment = align(reference_tokens, hypothesis_tokens)

    clinical_reference = [t for t in reference_tokens if is_clinical_token(t)]
    clinical_alignment = align(clinical_reference, [t for t in hypothesis_tokens if is_clinical_token(t)])

    return ItemScore(item_id=item.item_id, recording_id=item.recording_id, capture_device_class=item.capture_device_class, quality_bucket=item.quality_bucket, radiologist_id=item.radiologist_id, wer=alignment.wer(), insertion_rate=alignment.insertion_rate(), insertion_share=alignment.insertion_share_of_errors(), clinical_term_error_rate=clinical_alignment.wer() if clinical_reference else 0.0, latency_ms=latency_ms, code_switched_reference=has_non_latin_script(item.reference))


def _aggregate(scores: list[ItemScore]) -> dict[str, PartitionResult]:
    """Aggregate per capture partition — never across."""
    partitions: dict[str, list[ItemScore]] = defaultdict(list)
    for score in scores:
        key = "legacy" if score.capture_device_class == CaptureDeviceClass.LEGACY else "current"
        partitions[key].append(score)

    results: dict[str, PartitionResult] = {}
    for partition, bucket in partitions.items():
        by_quality: dict[str, list[float]] = defaultdict(list)
        by_speaker: dict[str, list[float]] = defaultdict(list)
        for score in bucket:
            by_quality[score.quality_bucket].append(score.wer)
            if score.radiologist_id is not None:
                by_speaker[str(score.radiologist_id)].append(score.wer)

        results[partition] = PartitionResult(partition=partition, item_count=len(bucket), wer=round(statistics.fmean(s.wer for s in bucket), 4), insertion_rate=round(statistics.fmean(s.insertion_rate for s in bucket), 4), insertion_share_of_errors=round(statistics.fmean(s.insertion_share for s in bucket), 4), clinical_term_error_rate=round(statistics.fmean(s.clinical_term_error_rate for s in bucket), 4), median_latency_ms=round(statistics.median(s.latency_ms for s in bucket), 1), by_quality={k: round(statistics.fmean(v), 4) for k, v in by_quality.items()}, by_speaker={k: round(statistics.fmean(v), 4) for k, v in by_speaker.items()})
    return results


def _code_switching_breakdown(scores: list[ItemScore]) -> dict[str, float]:
    """The probe, per speaker."""
    by_speaker: dict[str, list[float]] = defaultdict(list)
    for score in scores:
        if score.code_switched_reference and score.radiologist_id is not None:
            by_speaker[str(score.radiologist_id)].append(score.wer)
    return {k: round(statistics.fmean(v), 4) for k, v in by_speaker.items()}


def format_report(report: BakeoffReport) -> str:
    """A readable comparison — what actually gets pasted into the decision."""
    lines = [f"ASR bake-off — eval_set {report.eval_set_id} — {report.ran_at:%Y-%m-%d %H:%M} UTC", "", f"{'engine':28} {'ver':10} {'WER':>7} {'INS':>7} {'INS%err':>8} {'CTER':>7} {'n':>5}  partition", "-" * 92]
    for result in report.rank():
        for partition in ("current", "legacy"):
            p = result.partitions.get(partition)
            if p is None:
                continue
            flag = "  <- invents text" if p.invents_text else ""
            note = "" if partition == "current" else "  (archive only, not deciding)"
            lines.append(f"{result.engine:28.28} {result.engine_version:10.10} {p.wer:7.4f} {p.insertion_rate:7.4f} {p.insertion_share_of_errors:8.4f} {p.clinical_term_error_rate:7.4f} {p.item_count:5d}  {partition}{note}{flag}")
        if result.failures:
            lines.append(f"{'':40}{len(result.failures)} item(s) failed to transcribe")

    recommended = report.recommend()
    lines.append("")
    if recommended is None:
        lines.append("RECOMMENDATION: none. Every engine either produced no `current` results or invents text above the threshold. Choosing among these is a deliberate human decision, not a ranking.")
    else:
        lines.append(f"RECOMMENDATION: {recommended.engine} {recommended.engine_version} (on the `current` partition only)")

    switching = {r.engine: r.code_switching for r in report.engines if r.code_switching}
    if switching:
        lines.append("")
        lines.append("Code-switching probe — WER on code-switched items, per speaker:")
        for engine, per_speaker in switching.items():
            for speaker, wer in sorted(per_speaker.items()):
                lines.append(f"  {engine:28.28} speaker {speaker[:8]}  WER {wer:.4f}")
    return "\n".join(lines)
