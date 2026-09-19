"""Assembles the held-back set of reports accuracy is measured against, keeping archive and current-hardware audio apart.

Order: find eligible recordings (eligible_candidates, quality_bucket, resolve_tenant_slug) ->
assemble the set (assemble) -> freeze it so it cannot drift (freeze) -> prove none of it was
used for training (assert_no_training_leakage, partition_summary).
"""

from __future__ import annotations

import random
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core.errors import EvalSetLeakage
from radreport.core.logging import get_logger
from radreport.core.types import AudioQualityBucket, CaptureDeviceClass
from radreport.db.models.adaptation import VerbatimTranscript
from radreport.db.models.evaluation import EvalItem, EvalSet
from radreport.db.models.ingestion import Recording
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.tenancy import Tenant

log = get_logger(__name__)

#: Plan. The `current` partition is the critical path; `legacy` evaluates
#: archive reprocessing only and must never decide a forward-looking question.
CANONICAL_CURRENT_TARGET = 150
CANONICAL_LEGACY_TARGET = 100
#: the per-lab acceptance set.
ACCEPTANCE_TARGET = 40

#: measured at ingest; used here to stratify so a set is not accidentally
#: all-clean audio, which would flatter every engine equally and rank none.
_SNR_CLEAN_DB = 20.0
_SNR_MODERATE_DB = 12.0


def quality_bucket(snr_db: float | None) -> str:
    """The stratum. Unknown SNR is `moderate`, not `clean`."""
    if snr_db is None:
        return AudioQualityBucket.MODERATE
    if snr_db >= _SNR_CLEAN_DB:
        return AudioQualityBucket.CLEAN
    if snr_db >= _SNR_MODERATE_DB:
        return AudioQualityBucket.MODERATE
    return AudioQualityBucket.NOISY


@dataclass(frozen=True, slots=True)
class Candidate:
    recording_id: uuid.UUID
    tenant_id: uuid.UUID
    transcript_id: uuid.UUID
    verbatim: str
    capture_device_class: str
    quality_bucket: str
    duration_seconds: float
    radiologist_id: uuid.UUID
    includes_disfluencies: bool


@dataclass(slots=True)
class AssemblyResult:
    eval_set: EvalSet
    added: list[EvalItem] = field(default_factory=list)
    skipped: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    strata: Counter[tuple[str, str]] = field(default_factory=Counter)

    @property
    def by_device_class(self) -> Counter[str]:
        counts: Counter[str] = Counter()
        for (device_class, _quality), n in self.strata.items():
            counts[device_class] += n
        return counts


def eligible_candidates(session: Session, *, tenant_id: uuid.UUID | None = None) -> list[Candidate]:
    """Verbatim transcripts that could join a gold set."""
    stmt = select(VerbatimTranscript, Recording).join(Recording, Recording.id == VerbatimTranscript.recording_id).where(VerbatimTranscript.includes_disfluencies.is_(True))
    if tenant_id is not None:
        stmt = stmt.where(VerbatimTranscript.tenant_id == tenant_id)

    candidates: list[Candidate] = []
    for transcript, recording in session.execute(stmt).all():
        candidates.append(Candidate(recording_id=recording.id, tenant_id=recording.tenant_id, transcript_id=transcript.id, verbatim=transcript.text, capture_device_class=transcript.capture_device_class, quality_bucket=quality_bucket(float(recording.measured_snr_db) if recording.measured_snr_db is not None else None), duration_seconds=float(transcript.audio_duration_seconds or 0.0), radiologist_id=recording.radiologist_id, includes_disfluencies=transcript.includes_disfluencies))
    return candidates


def assemble(session: Session, *, eval_set: EvalSet, candidates: list[Candidate], target_current: int = CANONICAL_CURRENT_TARGET, target_legacy: int = CANONICAL_LEGACY_TARGET, seed: int = 0, actor_id: uuid.UUID | None = None) -> AssemblyResult:
    """Fill an eval set, stratified within each capture partition."""
    if eval_set.is_frozen:
        raise ValueError(f"eval_set {eval_set.id} is frozen; assemble a new version rather than adding to a set that release gates have already been measured against")

    existing = {row for row in session.execute(select(EvalItem.recording_id).where(EvalItem.eval_set_id == eval_set.id)).scalars().all()}
    result = AssemblyResult(eval_set=eval_set)
    rng = random.Random(seed)

    partitions = {CaptureDeviceClass.LEGACY: ([c for c in candidates if c.capture_device_class == CaptureDeviceClass.LEGACY], target_legacy), "current": ([c for c in candidates if c.capture_device_class != CaptureDeviceClass.LEGACY], target_current)}

    for partition, (pool, target) in partitions.items():
        available = [c for c in pool if c.recording_id not in existing]
        chosen = _sample_spread(available, target, rng)
        if len(chosen) < target:
            log.warning("eval_set_partition_short", eval_set_id=str(eval_set.id), partition=partition, chosen=len(chosen), target=target, detail="every metric on this partition carries wider error bars")

        for candidate in chosen:
            session.add(EvalItem(tenant_id=eval_set.tenant_id, eval_set_id=eval_set.id, recording_id=candidate.recording_id, source_tenant_id=candidate.tenant_id, gold_transcript_verbatim=candidate.verbatim, capture_device_class=candidate.capture_device_class, audio_quality_bucket=candidate.quality_bucket))
            result.strata[(candidate.capture_device_class, candidate.quality_bucket)] += 1

        _mark_excluded_from_training(session, [c.transcript_id for c in chosen])

    session.flush()
    result.added = list(session.execute(select(EvalItem).where(EvalItem.eval_set_id == eval_set.id)).scalars().all())

    session.add(AuditLog(tenant_id=eval_set.tenant_id, actor_id=actor_id, actor_type="user" if actor_id else "system", action="eval_set_assembled", entity_type="eval_set", entity_id=eval_set.id, after={"items": len(result.added), "by_device_class": dict(result.by_device_class), "seed": seed}))
    session.flush()

    log.info("eval_set_assembled", eval_set_id=str(eval_set.id), items=len(result.added), by_device_class=dict(result.by_device_class), strata={f"{d}/{q}": n for (d, q), n in result.strata.items()})
    return result


def _sample_spread(candidates: list[Candidate], target: int, rng: random.Random) -> list[Candidate]:
    """Round-robin across speakers, then across quality buckets."""
    if target <= 0 or not candidates:
        return []

    by_speaker: dict[uuid.UUID, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_speaker[candidate.radiologist_id].append(candidate)

    for pool in by_speaker.values():
        # Shuffle within a speaker by quality so a speaker's slice is not all
        # their earliest (and typically noisiest) recordings.
        rng.shuffle(pool)
        pool.sort(key=lambda c: _QUALITY_ORDER.index(c.quality_bucket))

    speakers = sorted(by_speaker, key=str)
    rng.shuffle(speakers)

    chosen: list[Candidate] = []
    cursor = {speaker: 0 for speaker in speakers}
    while len(chosen) < target:
        progressed = False
        for speaker in speakers:
            index = cursor[speaker]
            pool = by_speaker[speaker]
            if index >= len(pool):
                continue
            chosen.append(pool[index])
            cursor[speaker] = index + 1
            progressed = True
            if len(chosen) >= target:
                break
        if not progressed:
            break
    return chosen


#: Interleave clean → moderate → noisy so a truncated partition still spans
#: the quality range instead of being all-clean.
_QUALITY_ORDER = (AudioQualityBucket.CLEAN, AudioQualityBucket.MODERATE, AudioQualityBucket.NOISY)


def _mark_excluded_from_training(session: Session, transcript_ids: list[uuid.UUID]) -> None:
    """Eval membership permanently excludes a transcript from training."""
    if not transcript_ids:
        return
    for transcript in session.execute(select(VerbatimTranscript).where(VerbatimTranscript.id.in_(transcript_ids))).scalars().all():
        transcript.is_eval_set_member = True
    session.flush()


def freeze(session: Session, *, eval_set: EvalSet, actor_id: uuid.UUID | None = None, min_items: int | None = None) -> EvalSet:
    """Close a set to further changes. Every published number cites a frozen set."""
    items = list(session.execute(select(EvalItem).where(EvalItem.eval_set_id == eval_set.id)).scalars().all())
    floor = min_items if min_items is not None else (ACCEPTANCE_TARGET if eval_set.tenant_id is not None else CANONICAL_CURRENT_TARGET)
    if len(items) < floor:
        raise ValueError(f"eval_set {eval_set.id} holds {len(items)} items, below the floor of {floor}; freezing a short set publishes numbers with error bars nobody will re-read later")

    current = [i for i in items if i.capture_device_class != CaptureDeviceClass.LEGACY]
    if not current:
        raise ValueError("this set has no `current`-partition items, so it cannot answer any forward-looking question. A legacy-only set evaluates archive reprocessing and nothing else.")

    missing_gold = [str(i.id) for i in items if not i.gold_transcript_verbatim]
    if missing_gold:
        raise ValueError(f"{len(missing_gold)} item(s) have no verbatim reference; a gold set with unannotated items scores them as perfect or skips them, and both are wrong")

    eval_set.is_frozen = True
    session.add(AuditLog(tenant_id=eval_set.tenant_id, actor_id=actor_id, actor_type="user" if actor_id else "system", action="eval_set_frozen", entity_type="eval_set", entity_id=eval_set.id, after={"items": len(items), "current_items": len(current), "legacy_items": len(items) - len(current)}))
    session.flush()
    log.info("eval_set_frozen", eval_set_id=str(eval_set.id), items=len(items), current_items=len(current))
    return eval_set


def assert_no_training_leakage(session: Session, *, eval_set_id: uuid.UUID, training_recording_ids: list[uuid.UUID]) -> None:
    """'s check, for a training run that assembled its corpus elsewhere."""
    member_ids = {row for row in session.execute(select(EvalItem.recording_id).where(EvalItem.eval_set_id == eval_set_id)).scalars().all()}
    overlap = member_ids & set(training_recording_ids)
    if overlap:
        raise EvalSetLeakage(f"{len(overlap)} recording(s) are in eval_set {eval_set_id} and in the training corpus: {sorted(str(r) for r in overlap)[:5]}")


def partition_summary(session: Session, *, eval_set_id: uuid.UUID) -> dict[str, dict[str, int]]:
    """`{device_class: {quality_bucket: count}}` — the shape of a set."""
    rows = session.execute(select(EvalItem.capture_device_class, EvalItem.audio_quality_bucket).where(EvalItem.eval_set_id == eval_set_id)).all()

    summary: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for device_class, bucket in rows:
        summary[device_class][bucket or "unknown"] += 1
    return {k: dict(v) for k, v in summary.items()}


def resolve_tenant_slug(session: Session, tenant_id: uuid.UUID) -> str:
    tenant = session.get(Tenant, tenant_id)
    return tenant.slug if tenant else str(tenant_id)
