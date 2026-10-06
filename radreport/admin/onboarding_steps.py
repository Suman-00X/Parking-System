"""The onboarding steps a product admin runs for a lab, shared by the admin panel's pages and its JSON API.

Order: see where the lab stands (onboarding_overview) -> upload its data (import_roster_file,
submit_template_files, load_corpus_records) -> run the mining and seeding steps
(propose_template_merges, run_step over STEPS) -> fill and freeze the lab's acceptance set from
its verbatim transcripts (_assemble_acceptance, _freeze_acceptance), which the pilot gate needs.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radreport.core.types import ImportTrigger
from radreport.db.models.evaluation import EvalItem, EvalSet
from radreport.db.models.onboarding import ImportBatch
from radreport.eval import goldset
from radreport.onboarding import boilerplate, corpus, critical_rules, lexicon, paired_audio, roster, templates
from radreport.onboarding.batches import ArtifactUpload
from radreport.onboarding.readiness import evaluate_readiness


class StepRefused(Exception):
    """An onboarding step that cannot run, with the HTTP status the API should answer with."""

    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


def onboarding_overview(session: Session, tenant_id: uuid.UUID) -> dict[str, Any]:
    """One view of where this lab is across every onboarding stage."""
    verified, target = corpus.verification_progress(session, tenant_id)
    report = evaluate_readiness(session, tenant_id, persist=False)
    batches = list(session.execute(select(ImportBatch).where(ImportBatch.tenant_id == tenant_id).order_by(ImportBatch.created_at.desc()).limit(20)).scalars().all())
    return {
        "corpus_verification": {"verified": verified, "target": target},
        "gold_progress": {k: {"annotated": v[0], "target": v[1]} for k, v in paired_audio.gold_partition_progress(session, tenant_id=tenant_id).items()},
        "active_critical_rules": len(critical_rules.active_rules(session, tenant_id=tenant_id)),
        "recent_batches": [{"id": str(b.id), "batch_type": b.batch_type, "stage": b.stage, "status": b.status, "blocking_issue_count": b.blocking_issue_count, "accepted": b.accepted_count, "submitted_by": str(b.submitted_by) if b.submitted_by else None, "submitted_by_platform_user_id": str(b.submitted_by_platform_user_id) if b.submitted_by_platform_user_id else None} for b in batches],
        "readiness": {"passed": report.passed, "failures": [o.check_id for o in report.failures], "warnings": [o.check_id for o in report.warnings], "checks": [{"check_id": o.check_id, "status": o.status, "measured_value": o.measured_value, "threshold": o.threshold} for o in report.outcomes]},
    }


def import_roster_file(session: Session, tenant_id: uuid.UUID, data: bytes, *, trigger: str = ImportTrigger.INITIAL_ONBOARDING) -> dict[str, Any]:
    """Import the lab's roster from an HR CSV export."""
    rows, problems = roster.parse_roster_csv(data)
    if not rows and problems:
        raise StepRefused(422, "; ".join(problems))
    result = roster.import_roster(session, tenant_id=tenant_id, rows=rows, submitted_by=None, trigger=trigger)
    return {"batch_id": str(result.batch.id), "created": len(result.created), "updated": len(result.updated), "profiles_created": len(result.profiles_created), "problems": problems}


def submit_template_files(session: Session, tenant_id: uuid.UUID, uploads: list[ArtifactUpload], *, trigger: str = ImportTrigger.INITIAL_ONBOARDING) -> dict[str, Any]:
    """Parse uploaded documents into template candidates; nothing goes live."""
    if not uploads:
        raise StepRefused(422, "choose at least one template file")
    result = templates.submit_templates(session, tenant_id=tenant_id, uploads=uploads, submitted_by=None, trigger=trigger)
    return {"batch_id": str(result.batch.id), "candidates": len(result.candidates), "low_confidence": len(result.low_confidence), "failures": [{"filename": name, "reason": reason} for name, reason in result.failures]}


def load_corpus_records(session: Session, tenant_id: uuid.UUID, records: list[corpus.CorpusRecord], *, trigger: str = ImportTrigger.INITIAL_ONBOARDING) -> dict[str, Any]:
    """Bulk-load historical signed reports."""
    result = corpus.load_corpus(session, tenant_id=tenant_id, records=records, submitted_by=None, trigger=trigger)
    return {"batch_id": str(result.batch.id), "loaded": result.loaded, "duplicates": result.duplicates, "rejected": [{"id": rid, "reason": reason} for rid, reason in result.rejected]}


def propose_template_merges(session: Session, tenant_id: uuid.UUID, batch_id: uuid.UUID) -> dict[str, Any]:
    """Near-duplicate detection on one template batch, before routing is trained."""
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.tenant_id != tenant_id:
        raise StepRefused(404, f"no import batch {batch_id}")
    return {"proposals": len(templates.propose_merges(session, tenant_id=tenant_id, batch=batch))}


def _derive_map(session: Session, tenant_id: uuid.UUID, options: dict[str, Any]) -> dict[str, Any]:
    result = corpus.derive_template_map(session, tenant_id=tenant_id)
    verified, target = corpus.verification_progress(session, tenant_id)
    return {"mapped": result.mapped, "unmapped": result.unmapped, "by_method": dict(result.by_method), "verified": verified, "verification_target": target}


def _mine_lexicon(session: Session, tenant_id: uuid.UUID, options: dict[str, Any]) -> dict[str, Any]:
    result = lexicon.run_mining(session, tenant_id=tenant_id, min_frequency=int(options.get("min_frequency") or lexicon.DEFAULT_MIN_FREQUENCY), submitted_by=None)
    return {"batch_id": str(result.run.import_batch_id), "pass_number": result.run.pass_number, "terms_extracted": result.terms_extracted, "terms_new": result.terms_new, "terms_pending_review": result.terms_pending_review, "ambiguous": result.ambiguous}


def _collision_audit(session: Session, tenant_id: uuid.UUID, options: dict[str, Any]) -> dict[str, Any]:
    findings = lexicon.run_collision_audit(session, tenant_id=tenant_id)
    return {"new_findings": len(findings), "blocking": sum(1 for f in findings if f.severity == "block"), "findings": [{"id": str(f.id), "label_a": f.label_a, "label_b": f.label_b, "distance": float(f.phonetic_distance or 0.0), "collision_class": f.collision_class, "severity": f.severity} for f in findings]}


def _mine_variants(session: Session, tenant_id: uuid.UUID, options: dict[str, Any]) -> dict[str, Any]:
    result = paired_audio.mine_surface_variants(session, tenant_id=tenant_id, submitted_by=None)
    return {"transcripts_scanned": result.transcripts_scanned, "variants_written": result.variants_written, "terms_touched": result.terms_touched, "unmatched_frequent": [{"surface": s, "count": n} for s, n in result.unmatched_frequent]}


def _mine_boilerplate(session: Session, tenant_id: uuid.UUID, options: dict[str, Any]) -> dict[str, Any]:
    result = boilerplate.mine_boilerplate(session, tenant_id=tenant_id, verified_only=bool(options.get("verified_only", False)))
    return {"candidates_written": result.candidates_written, "fields_scanned": result.fields_scanned, "per_template": result.per_template}


def _seed_critical_rules(session: Session, tenant_id: uuid.UUID, options: dict[str, Any]) -> dict[str, Any]:
    result = critical_rules.seed_candidate_rules(session, tenant_id=tenant_id, actor_id=None)
    return {"created": [r.code for r in result.created], "candidates": [{"code": c.code, "finding_label": c.finding_label, "severity": c.severity, "corpus_mentions": c.corpus_mentions, "examples": list(c.example_sentences)} for c in result.candidates], "lab_specific_phrases": [{"phrase": p, "count": n} for p, n in result.lab_specific_phrases]}


def _acceptance_set(session: Session, tenant_id: uuid.UUID) -> EvalSet:
    """The lab's acceptance set that can still change, created if registration predates it."""
    sets = list(session.execute(select(EvalSet).where(EvalSet.tenant_id == tenant_id, EvalSet.is_canonical.is_(False)).order_by(EvalSet.created_at.desc())).scalars().all())
    open_set = next((s for s in sets if not s.is_frozen), None)
    if open_set is not None:
        return open_set
    if sets:
        raise StepRefused(409, "the acceptance set is already frozen; readiness is measured against it and it cannot change")
    created = EvalSet(tenant_id=tenant_id, name=f"acceptance-{tenant_id.hex[:8]}", description="Per-lab acceptance set for the pilot gate; not a release gate.", is_frozen=False, is_canonical=False, stratification_spec={"target_size": goldset.ACCEPTANCE_TARGET, "stratify_by": ["capture_device_class", "audio_quality_bucket"]})
    session.add(created)
    session.flush()
    return created


def _assemble_acceptance(session: Session, tenant_id: uuid.UUID, options: dict[str, Any]) -> dict[str, Any]:
    eval_set = _acceptance_set(session, tenant_id)
    # Current hardware only: the pilot question is how the lab's new microphones perform.
    result = goldset.assemble(session, eval_set=eval_set, candidates=goldset.eligible_candidates(session, tenant_id=tenant_id), target_current=goldset.ACCEPTANCE_TARGET, target_legacy=0)
    held = len(result.added)
    return {"eval_set_id": str(eval_set.id), "items": held, "target": goldset.ACCEPTANCE_TARGET, "short_by": max(0, goldset.ACCEPTANCE_TARGET - held)}


def _freeze_acceptance(session: Session, tenant_id: uuid.UUID, options: dict[str, Any]) -> dict[str, Any]:
    eval_set = _acceptance_set(session, tenant_id)
    try:
        goldset.freeze(session, eval_set=eval_set)
    except ValueError as exc:
        raise StepRefused(409, str(exc)) from exc
    items = session.execute(select(func.count()).select_from(EvalItem).where(EvalItem.eval_set_id == eval_set.id)).scalar_one()
    return {"eval_set_id": str(eval_set.id), "items": items, "frozen": True}


#: Step name -> (label shown in the panel, what it does). Each takes optional `options`.
STEPS: dict[str, tuple[str, Callable[[Session, uuid.UUID, dict[str, Any]], dict[str, Any]]]] = {"derive-map": ("Derive the report-to-template map", _derive_map), "lexicon-mine": ("Mine terms from the corpus", _mine_lexicon), "collision-audit": ("Run the sound-alike collision audit", _collision_audit), "mine-variants": ("Mine what the ASR actually heard", _mine_variants), "boilerplate-mine": ("Rank normal statements", _mine_boilerplate), "critical-rules-seed": ("Propose critical-finding rules", _seed_critical_rules), "acceptance-assemble": ("Fill the acceptance set from verbatim transcripts", _assemble_acceptance), "acceptance-freeze": ("Freeze the acceptance set", _freeze_acceptance)}


def run_step(session: Session, tenant_id: uuid.UUID, step: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one named mining or seeding step."""
    if step not in STEPS:
        raise StepRefused(404, f"no onboarding step {step!r}; choose one of {', '.join(STEPS)}")
    return STEPS[step][1](session, tenant_id, options or {})
