"""The six checks that must all pass before any audio is used to train a speech model.

Order: each gate runs on its own -- enough hours (gate_g1_volume), a reserved second check
(gate_g2_unknown), speakers spread evenly (gate_g3_speaker_balance), one kind of microphone
(gate_g4_hardware_homogeneous), a reserved fifth check (gate_g5_unknown) and consent on every
item (gate_g6_legal_basis). evaluate_gates runs all six; require_gates refuses to go on unless
they pass.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.types import AdaptationTarget, CaptureDeviceClass
from radreport.db.models.adaptation import VerbatimTranscript
from radreport.db.models.ingestion import Recording
from radreport.knowledge.consent import verify_g6_legal_basis

log = get_logger(__name__)

GLOBAL_ADAPTER_HOURS = 20.0
SPEAKER_ADAPTER_HOURS = 5.0

#: G3: ≥5 distinct dictating radiologists, none above 40% of corpus hours.
MIN_SPEAKERS = 5
MAX_SPEAKER_SHARE = 0.40

#: G4: the corpus must not mix capture hardware.
MIN_DEVICE_CLASS_SHARE = 0.90


@dataclass(frozen=True, slots=True)
class GateResult:
    gate_id: str
    passed: bool
    reason: str
    measured: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class GateReport:
    target: str
    results: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[GateResult]:
        return [r for r in self.results if not r.passed]

    def as_json(self) -> dict[str, object]:
        """For `model_adaptation_run.prerequisite_gates_passed`."""
        return {"target": self.target, "all_passed": self.passed, "gates": {r.gate_id: {"passed": r.passed, "reason": r.reason, "measured": r.measured} for r in self.results}}


@dataclass(frozen=True, slots=True)
class _CorpusItem:
    recording_id: uuid.UUID
    radiologist_id: uuid.UUID
    hours: float
    capture_device_class: str
    includes_disfluencies: bool
    is_eval_set_member: bool


def _load_corpus(session: Session, recording_ids: list[uuid.UUID]) -> list[_CorpusItem]:
    rows = session.execute(select(VerbatimTranscript, Recording).join(Recording, Recording.id == VerbatimTranscript.recording_id).where(VerbatimTranscript.recording_id.in_(recording_ids or [None]))).all()
    return [_CorpusItem(recording_id=recording.id, radiologist_id=recording.radiologist_id, hours=float(transcript.audio_duration_seconds or 0.0) / 3600.0, capture_device_class=transcript.capture_device_class, includes_disfluencies=transcript.includes_disfluencies, is_eval_set_member=transcript.is_eval_set_member) for transcript, recording in rows]


def gate_g1_volume(corpus: list[_CorpusItem], *, target: str) -> GateResult:
    """≥20 h for a global adapter, ≥5 h per speaker for a per-speaker one."""
    usable = [c for c in corpus if c.includes_disfluencies and not c.is_eval_set_member]
    hours = round(sum(c.hours for c in usable), 3)
    required = SPEAKER_ADAPTER_HOURS if target == AdaptationTarget.ASR_SPEAKER else GLOBAL_ADAPTER_HOURS
    excluded = len(corpus) - len(usable)
    return GateResult(gate_id="G1_volume", passed=hours >= required, reason=(f"{hours:.2f} h of usable verbatim against {required:.0f} h required" + (f"; {excluded} item(s) excluded as cleaned or eval-set" if excluded else "")), measured={"hours": hours, "required_hours": required, "excluded_items": excluded})


def gate_g2_unknown() -> GateResult:
    """**Not implemented.** The second gate; its text is not in `PLAN.md`."""
    return GateResult(gate_id="G2_not_implemented", passed=False, reason=("The second gate is not implemented: its condition is not stated in PLAN.md and was not recoverable from the design PDF. Transcribe it and implement the check before adapting."), measured={"status": "unimplemented"})


def gate_g3_speaker_balance(corpus: list[_CorpusItem], *, target: str) -> GateResult:
    """≥5 speakers, none above 40% of corpus hours. **Global adapter only.**"""
    if target == AdaptationTarget.ASR_SPEAKER:
        return GateResult(gate_id="G3_speaker_balance", passed=True, reason="not applicable to a per-speaker adapter", measured={"applicable": False})

    usable = [c for c in corpus if c.includes_disfluencies and not c.is_eval_set_member]
    by_speaker: dict[uuid.UUID, float] = defaultdict(float)
    for item in usable:
        by_speaker[item.radiologist_id] += item.hours

    total = sum(by_speaker.values())
    speakers = len(by_speaker)
    max_share = (max(by_speaker.values()) / total) if total > 0 else 1.0

    passed = speakers >= MIN_SPEAKERS and max_share <= MAX_SPEAKER_SHARE
    return GateResult(gate_id="G3_speaker_balance", passed=passed, reason=(f"{speakers} speaker(s) against {MIN_SPEAKERS} required; largest holds {max_share:.1%} of hours against a {MAX_SPEAKER_SHARE:.0%} ceiling"), measured={"speakers": speakers, "max_speaker_share": round(max_share, 4), "required_speakers": MIN_SPEAKERS})


def gate_g4_hardware_homogeneous(corpus: list[_CorpusItem]) -> GateResult:
    """The corpus must not mix capture hardware."""
    usable = [c for c in corpus if c.includes_disfluencies and not c.is_eval_set_member]
    by_class: dict[str, float] = defaultdict(float)
    for item in usable:
        by_class[item.capture_device_class] += item.hours

    total = sum(by_class.values())
    if total <= 0:
        return GateResult(gate_id="G4_hardware_homogeneous", passed=False, reason="no usable corpus hours to assess", measured={})

    dominant_class, dominant_hours = max(by_class.items(), key=lambda kv: kv[1])
    share = dominant_hours / total
    return GateResult(
        gate_id="G4_hardware_homogeneous",
        passed=share >= MIN_DEVICE_CLASS_SHARE,
        reason=(f"{share:.1%} of hours are {dominant_class!r} against a {MIN_DEVICE_CLASS_SHARE:.0%} floor"),
        measured={
            "dominant_class": dominant_class,
            "share": round(share, 4),
            "classes": {k: round(v, 3) for k, v in by_class.items()},
            # Stated separately because this is the mistake the gate exists to
            # catch, and the number is the argument.
            "legacy_hours": round(by_class.get(CaptureDeviceClass.LEGACY, 0.0), 3),
        },
    )


def gate_g5_unknown() -> GateResult:
    """**Not implemented.** The fifth gate; see `gate_g2_unknown`."""
    return GateResult(gate_id="G5_not_implemented", passed=False, reason=("The fifth gate is not implemented: its condition is not stated in PLAN.md and was not recoverable from the design PDF. Transcribe it and implement the check before adapting."), measured={"status": "unimplemented"})


def gate_g6_legal_basis(session: Session, recording_ids: list[uuid.UUID]) -> GateResult:
    """Consent was live, per speaker and per tenant, and the PHI scrub ran."""
    passed, problems = verify_g6_legal_basis(session, recording_ids)
    return GateResult(gate_id="G6_legal_basis", passed=passed, reason=("tenant consent live at upload, speaker consent present, PHI scrub complete for every item" if passed else f"{sum(len(v) for v in problems.values())} item(s) lack a legal basis"), measured={k: v[:5] for k, v in problems.items()})


def evaluate_gates(session: Session, *, recording_ids: list[uuid.UUID], target: str = AdaptationTarget.ASR_GLOBAL) -> GateReport:
    """Run all six gates. Adaptation proceeds only if every one passes."""
    corpus = _load_corpus(session, recording_ids)
    report = GateReport(target=target)
    report.results = [gate_g1_volume(corpus, target=target), gate_g2_unknown(), gate_g3_speaker_balance(corpus, target=target), gate_g4_hardware_homogeneous(corpus), gate_g5_unknown(), gate_g6_legal_basis(session, recording_ids)]

    log.info("adaptation_gates_evaluated", target=target, items=len(corpus), passed=report.passed, failures=[r.gate_id for r in report.failures])
    return report


class AdaptationBlocked(Exception):
    """One or more gates failed."""

    def __init__(self, report: GateReport) -> None:
        super().__init__("adaptation blocked by " + ", ".join(f"{r.gate_id} ({r.reason})" for r in report.failures))
        self.report = report


def require_gates(session: Session, *, recording_ids: list[uuid.UUID], target: str = AdaptationTarget.ASR_GLOBAL) -> GateReport:
    """Evaluate and raise unless all six pass."""
    report = evaluate_gates(session, recording_ids=recording_ids, target=target)
    if not report.passed:
        raise AdaptationBlocked(report)
    return report
