"""Pairs recorded dictation with a transcriptionist's typed verbatim: the ground truth every accuracy number is measured against.

Order: build the annotation queue (build_verbatim_queue) -> tie each item to its corpus report
(link_corpus_report) -> submit the typed verbatim (submit_verbatim) -> track hours and coverage
(corpus_hours, gold_partition_progress) -> mine the ways people actually say each term
(mine_surface_variants).
"""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.system_config import adapter_thresholds
from radreport.core.types import ActorType, CaptureDeviceClass, ImportBatchType, ImportStatus, ImportTrigger, TermType, VariantSource, VerbatimSource
from radreport.db.models.adaptation import VerbatimTranscript
from radreport.db.models.identity import Study
from radreport.db.models.ingestion import Recording
from radreport.db.models.knowledge import LexiconSet, LexiconTerm
from radreport.db.models.onboarding import CorpusReport, ImportBatch
from radreport.db.models.orchestration import AuditLog
from radreport.knowledge import variant_review
from radreport.knowledge.lexicon_versions import current_set
from radreport.knowledge.phonetics import double_metaphone, phonetic_distance
from radreport.onboarding.batches import open_batch, record_counts, transition
from radreport.onboarding.lexicon import record_surface_variants_bulk

log = get_logger(__name__)

#: the `G1_volume` gate: ≥20 h for a global adapter, ≥5 h per speaker.
GLOBAL_ADAPTER_HOURS = 20.0
SPEAKER_ADAPTER_HOURS = 5.0

#: the gold partitions. 150 `current`, 100 `legacy`.
CURRENT_GOLD_TARGET = 150
LEGACY_GOLD_TARGET = 100

#: A mined variant must be this close phonetically to count as the same term.
VARIANT_MAX_DISTANCE = 0.12
#: How far apart a heard phrase and a term may sound and still be scored at all; the thresholds then decide what is used.
CANDIDATE_MAX_DISTANCE = 0.45

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


@dataclass(frozen=True, slots=True)
class QueueItem:
    recording_id: uuid.UUID
    study_id: uuid.UUID
    capture_device_class: str
    duration_seconds: float
    radiologist_id: uuid.UUID
    has_verbatim: bool


@dataclass(slots=True)
class VerbatimQueue:
    """What the transcription team is looking at, split the way requires."""

    current: list[QueueItem] = field(default_factory=list)
    legacy: list[QueueItem] = field(default_factory=list)

    @property
    def total_outstanding(self) -> int:
        return sum(1 for item in self.current + self.legacy if not item.has_verbatim)


def build_verbatim_queue(session: Session, *, tenant_id: uuid.UUID, limit: int | None = None) -> VerbatimQueue:
    """Recordings awaiting verbatim annotation, `current` first."""
    annotated = {row for row in session.execute(select(VerbatimTranscript.recording_id).where(VerbatimTranscript.tenant_id == tenant_id)).scalars().all()}

    rows = list(session.execute(select(Recording).where(Recording.tenant_id == tenant_id).order_by(Recording.uploaded_at.asc())).scalars().all())

    queue = VerbatimQueue()
    for recording in rows:
        item = QueueItem(recording_id=recording.id, study_id=recording.study_id, capture_device_class=recording.capture_device_class, duration_seconds=float(recording.duration_seconds or 0.0), radiologist_id=recording.radiologist_id, has_verbatim=recording.id in annotated)
        if recording.capture_device_class == CaptureDeviceClass.LEGACY:
            queue.legacy.append(item)
        else:
            queue.current.append(item)

    if limit is not None:
        queue.current = queue.current[:limit]
        queue.legacy = queue.legacy[:limit]
    return queue


def link_corpus_report(session: Session, *, tenant_id: uuid.UUID, recording_id: uuid.UUID, external_report_id: str) -> CorpusReport:
    """Pair a recording with the signed report it produced."""
    recording = session.get(Recording, recording_id)
    if recording is None or recording.tenant_id != tenant_id:
        raise ValueError(f"no recording {recording_id} in this tenant")

    report = session.execute(select(CorpusReport).where(CorpusReport.tenant_id == tenant_id, CorpusReport.external_report_id == external_report_id)).scalar_one_or_none()
    if report is None:
        raise ValueError(f"no corpus_report with external id {external_report_id!r}")

    study = session.get(Study, recording.study_id)
    if study is not None and not study.accession_number:
        study.accession_number = external_report_id

    session.flush()
    log.info("s4_recording_linked", tenant_id=str(tenant_id), recording_id=str(recording_id), external_report_id=external_report_id)
    return report


def submit_verbatim(session: Session, *, tenant_id: uuid.UUID, recording_id: uuid.UUID, text: str, annotator_id: uuid.UUID, includes_disfluencies: bool, source: str = VerbatimSource.HUMAN_ANNOTATION, is_eval_set_member: bool = False, batch: ImportBatch | None = None) -> VerbatimTranscript:
    """Record one human-produced verbatim transcript."""
    recording = session.get(Recording, recording_id)
    if recording is None or recording.tenant_id != tenant_id:
        raise ValueError(f"no recording {recording_id} in this tenant")
    if not text.strip():
        raise ValueError("a verbatim transcript cannot be empty")

    existing = session.execute(select(VerbatimTranscript).where(VerbatimTranscript.tenant_id == tenant_id, VerbatimTranscript.recording_id == recording_id, VerbatimTranscript.source == source)).scalar_one_or_none()
    if existing is not None:
        existing.text = text
        existing.includes_disfluencies = includes_disfluencies
        existing.annotator_id = annotator_id
        session.flush()
        return existing

    transcript = VerbatimTranscript(tenant_id=tenant_id, recording_id=recording_id, text=text, source=source, annotator_id=annotator_id, includes_disfluencies=includes_disfluencies, audio_duration_seconds=float(recording.duration_seconds or 0.0), capture_device_class=recording.capture_device_class, is_eval_set_member=is_eval_set_member, quality_checked=False)
    session.add(transcript)

    if not includes_disfluencies:
        # Not an error — a cleaned transcript is still a usable eval reference.
        log.warning("s4_verbatim_without_disfluencies", tenant_id=str(tenant_id), recording_id=str(recording_id), detail="not ASR-training eligible: a cleaned transcript teaches deletion")

    session.add(AuditLog(tenant_id=tenant_id, actor_id=annotator_id, actor_type=ActorType.USER, action="verbatim_transcript_submitted", entity_type="recording", entity_id=recording_id, after={"source": source, "includes_disfluencies": includes_disfluencies, "capture_device_class": recording.capture_device_class}))
    session.flush()
    if batch is not None:
        record_counts(session, batch, accepted=1)
    return transcript


@dataclass(frozen=True, slots=True)
class CorpusHours:
    """The `G1_volume` accounting, split the way decisions are made."""

    current_hours: float
    legacy_hours: float
    training_eligible_hours: float
    per_speaker_hours: dict[str, float]
    global_floor_hours: float = GLOBAL_ADAPTER_HOURS
    speaker_floor_hours: float = SPEAKER_ADAPTER_HOURS
    """Both floors as configured for the lab when counted, so this agrees with the gates."""

    @property
    def meets_global_adapter_floor(self) -> bool:
        return self.training_eligible_hours >= self.global_floor_hours

    def speakers_meeting_floor(self) -> list[str]:
        return [s for s, h in self.per_speaker_hours.items() if h >= self.speaker_floor_hours]


def corpus_hours(session: Session, *, tenant_id: uuid.UUID) -> CorpusHours:
    """How many hours of verbatim exist, and how many can legally train."""
    rows = session.execute(select(VerbatimTranscript.capture_device_class, VerbatimTranscript.audio_duration_seconds, VerbatimTranscript.includes_disfluencies, VerbatimTranscript.is_eval_set_member, Recording.radiologist_id).join(Recording, Recording.id == VerbatimTranscript.recording_id).where(VerbatimTranscript.tenant_id == tenant_id)).all()

    current = legacy = eligible = 0.0
    per_speaker: dict[str, float] = defaultdict(float)
    for device_class, seconds, disfluencies, eval_member, radiologist_id in rows:
        hours = float(seconds or 0.0) / 3600.0
        if device_class == CaptureDeviceClass.LEGACY:
            legacy += hours
        else:
            current += hours
        # Eval membership is a permanent training exclusion.
        if disfluencies and not eval_member:
            eligible += hours
            per_speaker[str(radiologist_id)] += hours

    floors = adapter_thresholds(session, tenant_id=tenant_id)
    return CorpusHours(current_hours=round(current, 3), legacy_hours=round(legacy, 3), training_eligible_hours=round(eligible, 3), per_speaker_hours={k: round(v, 3) for k, v in per_speaker.items()}, global_floor_hours=floors.global_hours, speaker_floor_hours=floors.speaker_hours)


def gold_partition_progress(session: Session, *, tenant_id: uuid.UUID) -> dict[str, tuple[int, int]]:
    """`{partition: (annotated, target)}` — the critical path, as a number."""
    rows = session.execute(select(VerbatimTranscript.capture_device_class, func.count()).where(VerbatimTranscript.tenant_id == tenant_id).group_by(VerbatimTranscript.capture_device_class)).all()
    counts: dict[str, int] = {cls: int(n) for cls, n in rows}
    current = sum(n for cls, n in counts.items() if cls != CaptureDeviceClass.LEGACY)
    return {"current": (current, CURRENT_GOLD_TARGET), "legacy": (counts.get(CaptureDeviceClass.LEGACY, 0), LEGACY_GOLD_TARGET)}


# --------------------- term mining <-> verbatim annotation variant loop -----
@dataclass(slots=True)
class VariantMiningResult:
    batch: ImportBatch
    transcripts_scanned: int = 0
    variants_written: int = 0
    terms_touched: int = 0
    auto_approved: int = 0
    pending_review: int = 0
    hidden: int = 0
    """Matches too weak to show anyone; counted so the threshold's effect is visible."""
    unmatched_frequent: list[tuple[str, int]] = field(default_factory=list)
    """High-frequency surfaces that matched no term."""


def mine_surface_variants(session: Session, *, tenant_id: uuid.UUID, min_occurrences: int = 2, submitted_by: uuid.UUID | None = None, batch: ImportBatch | None = None) -> VariantMiningResult:
    """Mine how terms actually come back from ASR, and write them to term mining."""
    batch = batch or open_batch(session, tenant_id=tenant_id, batch_type=ImportBatchType.PAIRED_AUDIO, stage="S4", trigger=ImportTrigger.PERIODIC_REMINE, submitted_by=submitted_by)
    if batch.status == ImportStatus.UPLOADING:
        transition(session, batch, ImportStatus.PARSING, actor_id=submitted_by)

    result = VariantMiningResult(batch=batch)

    terms = list(session.execute(select(LexiconTerm).join(LexiconSet, LexiconSet.id == LexiconTerm.lexicon_set_id).where(LexiconSet.tenant_id == tenant_id, current_set(), LexiconTerm.term_type.in_((TermType.ABBREVIATION, TermType.CODE_WORD, TermType.ANATOMY)))).scalars().all())
    if not terms:
        log.warning("s4_no_terms_to_match", tenant_id=str(tenant_id))
        return result

    texts = list(session.execute(select(VerbatimTranscript.text).where(VerbatimTranscript.tenant_id == tenant_id)).scalars().all())
    result.transcripts_scanned = len(texts)

    surfaces: Counter[str] = Counter()
    for text in texts:
        tokens = _WORD.findall(text)
        surfaces.update(tokens)
        # Two- and three-word windows, because "left main coronary" is one term
        # said as three words and no unigram scan will ever see it.
        for size in (2, 3):
            for i in range(len(tokens) - size + 1):
                surfaces[" ".join(tokens[i : i + size])] += 1

    by_key: dict[str, list[LexiconTerm]] = defaultdict(list)
    for term in terms:
        by_key[term.phonetic_key_primary].append(term)
        if term.phonetic_key_secondary:
            by_key[term.phonetic_key_secondary].append(term)

    per_term: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
    meta: dict[tuple[uuid.UUID, str], dict[str, object]] = {}
    limits_by_type: dict[str, variant_review.Thresholds] = {}
    for surface, count in surfaces.items():
        if count < min_occurrences:
            continue
        key, _ = double_metaphone(surface)
        matches = by_key.get(key, [])
        if not matches:
            if count >= min_occurrences * 5 and len(surface.split()) == 1:
                result.unmatched_frequent.append((surface, count))
            continue

        for term in matches:
            if surface.lower() == term.canonical_form.lower():
                continue
            if phonetic_distance(surface, term.canonical_form) > CANDIDATE_MAX_DISTANCE:
                continue
            confidence = variant_review.match_confidence(surface, term.canonical_form)
            limits = limits_by_type.setdefault(term.term_type, variant_review.thresholds(session, tenant_id, term.term_type))
            status = variant_review.decide(confidence, limits)
            if status is None:
                result.hidden += 1
                continue
            per_term[term.id][surface] = count
            meta[(term.id, surface)] = {"confidence": confidence, "review_status": status, "threshold_arm": limits.arm}
            if status == "auto_approved":
                result.auto_approved += 1
            else:
                result.pending_review += 1

    result.variants_written += record_surface_variants_bulk(session, tenant_id=tenant_id, per_term=dict(per_term), source=VariantSource.MINED, meta=meta)
    auto = [(term_id, surface, m["confidence"]) for (term_id, surface), m in meta.items() if m["review_status"] == "auto_approved"]
    if auto:
        # Every automatic approval is on the record, so a radiologist can audit what was let through unasked.
        session.add_all(AuditLog(tenant_id=tenant_id, actor_id=None, actor_type=ActorType.SYSTEM, action="lexicon_variant_auto_approved", entity_type="lexicon_term", entity_id=term_id, after={"surface": surface, "confidence": confidence}) for term_id, surface, confidence in auto)
    result.terms_touched = len(per_term)
    result.unmatched_frequent.sort(key=lambda pair: -pair[1])
    result.unmatched_frequent = result.unmatched_frequent[:50]

    session.flush()
    log.info("s4_variants_mined", tenant_id=str(tenant_id), batch_id=str(batch.id), transcripts=result.transcripts_scanned, variants_written=result.variants_written, terms_touched=result.terms_touched, unmatched_frequent=len(result.unmatched_frequent))
    return result
