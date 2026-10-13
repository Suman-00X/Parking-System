"""The onboarding steps a product admin runs for a lab, shared by the admin panel's pages and its JSON API.

Order: see where the lab stands (onboarding_overview, with the nine steps in PROCESS) -> roster
(import_roster_file) -> templates and the shorthand sheet (submit_template_files,
submit_shorthand_files, propose_template_merges) -> gold bundles for every template
(import_gold_bundle_file), then the acceptance set filled per template and frozen
(_assemble_acceptance, _freeze_acceptance) -> old signed reports (load_corpus_records,
load_corpus_file) and the mining steps that read them (run_step over STEPS) -> collisions and
critical rules -> the baseline audit per autonomy class (baseline_sample_csv,
record_baseline_grades, measure_baseline) -> the readiness gate.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from radreport.adapters.storage.object_store import ObjectStore, object_store
from radreport.autonomy import baseline_audit
from radreport.core import system_config
from radreport.core.config import get_settings
from radreport.core.types import ImportTrigger
from radreport.db.models.evaluation import EvalItem, EvalSet
from radreport.db.models.onboarding import CorpusReport, CorpusReportTemplateMap, ImportBatch
from radreport.eval import goldset
from radreport.onboarding import boilerplate, corpus, critical_rules, gold_bundles, lexicon, paired_audio, roster, shorthand, templates
from radreport.onboarding.batches import ArtifactUpload
from radreport.onboarding.readiness import evaluate_readiness


class StepRefused(Exception):
    """An onboarding step that cannot run, with the HTTP status the API should answer with."""

    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


#: The nine onboarding steps in order: (number, title, what happens, the STEPS run within it).
PROCESS: tuple[tuple[int, str, str, tuple[str, ...]], ...] = (
    (1, "Register the lab", "The lab, its first lab admin and its empty acceptance set.", ()),
    (2, "Roster and voice consent", "Every person at the lab; each radiologist then enrolls a voiceprint with signed consent.", ()),
    (3, "Templates and shortcuts", "One template per examination, plus the shortcut sheet; the lab's radiologists approve each template.", ()),
    (4, "Gold bundles per template", "For every template, dictations with their verbatim transcript and final report, held back as the acceptance set.", ("acceptance-assemble", "acceptance-freeze")),
    (5, "Old signed reports", "Text only, as many as the lab has; they build the dictionary, the normal sentences and the audit sample.", ("derive-map", "lexicon-mine", "mine-variants", "boilerplate-mine", "radlex-annotate")),
    (6, "Collisions and critical rules", "Sound-alike terms the lab's radiologists settle; critical-finding rules when alerts are switched on.", ("collision-audit", "critical-rules-seed")),
    (7, "Baseline audit", "An independent grader re-reads a sample of signed reports per autonomy class; the error rate comes from those grades.", ()),
    (8, "Readiness gate", "The seven checks; all blocking ones must pass to start the pilot.", ()),
    (9, "Pilot, then live", "The lab dictates for real with every report reviewed; going live is a later decision.", ()),
)


def gold_per_template(session: Session, tenant_id: uuid.UUID) -> int:
    """Gold bundles each active template needs."""
    return int(system_config.resolve(session, "onboarding.gold_per_template", tenant_id=tenant_id).value)


def onboarding_overview(session: Session, tenant_id: uuid.UUID) -> dict[str, Any]:
    """One view of where this lab is across every onboarding step."""
    report = evaluate_readiness(session, tenant_id, persist=False)
    by_id = {o.check_id: o for o in report.outcomes}
    reports, verified = session.execute(select(select(func.count()).select_from(CorpusReport).where(CorpusReport.tenant_id == tenant_id).scalar_subquery(), select(func.count()).select_from(CorpusReportTemplateMap).where(CorpusReportTemplateMap.tenant_id == tenant_id, CorpusReportTemplateMap.is_verified.is_(True)).scalar_subquery())).one()
    per = gold_per_template(session, tenant_id)
    bundles = gold_bundles.bundles_per_template(session, tenant_id)
    batches = list(session.execute(batches_query(tenant_id).limit(20)).scalars().all())
    floor = baseline_audit.min_graded(session, tenant_id)
    baselines = [{"code": code, "status": status, "graded": graded, "cse": cse, "min_graded": floor, "baseline_cse_rate": float(rate or 0)} for code, status, rate, graded, cse in baseline_audit.progress_by_class(session, tenant_id)]
    # The readiness checks already counted these; reading them again would double the page's queries.
    rules = by_id["critical_rules_approved"].detail or {}
    return {
        "gold_bundles": {"per_template": per, "templates": bundles, "complete": sum(1 for n in bundles.values() if n >= per), "active_templates": len(bundles)},
        "corpus_reports": int(reports),
        "corpus_verification": {"verified": int(verified), "target": corpus.VERIFIED_MAPPING_TARGET},
        "critical_alerts_on": bool(rules.get("alerts_on")),
        "active_critical_rules": int(rules.get("approved_rules", 0)),
        "baselines": baselines,
        "recent_batches": [batch_summary(b) for b in batches],
        "readiness": {"passed": report.passed, "failures": [o.check_id for o in report.failures], "warnings": [o.check_id for o in report.warnings], "checks": [{"check_id": o.check_id, "status": o.status, "measured_value": o.measured_value, "threshold": o.threshold} for o in report.outcomes]},
    }


def batches_query(tenant_id: uuid.UUID, *, batch_type: str | None = None) -> Select[tuple[ImportBatch]]:
    """A lab's import batches, newest first, in a stable order for paging."""
    query = select(ImportBatch).where(ImportBatch.tenant_id == tenant_id)
    if batch_type:
        query = query.where(ImportBatch.batch_type == batch_type)
    return query.order_by(ImportBatch.created_at.desc(), ImportBatch.id)


def batch_summary(b: ImportBatch) -> dict[str, Any]:
    """The fields a batch list shows."""
    return {"id": str(b.id), "batch_type": b.batch_type, "stage": b.stage, "status": b.status, "blocking_issue_count": b.blocking_issue_count, "accepted": b.accepted_count, "rejected": b.rejected_count, "items": b.item_count, "created_at": b.created_at.isoformat() if b.created_at else None, "submitted_by": str(b.submitted_by) if b.submitted_by else None, "submitted_by_platform_user_id": str(b.submitted_by_platform_user_id) if b.submitted_by_platform_user_id else None}


def batch_status(session: Session, tenant_id: uuid.UUID, batch_id: uuid.UUID) -> dict[str, Any]:
    """One batch and what it holds, counted with one grouped query per child table rather than per row."""
    from radreport.db.models.onboarding import CollisionAuditFinding, CorpusReport, ImportArtifact, TemplateImportCandidate

    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.tenant_id != tenant_id:
        raise StepRefused(404, f"no import_batch {batch_id} in this lab")
    candidates = dict(session.execute(select(TemplateImportCandidate.review_status, func.count()).where(TemplateImportCandidate.tenant_id == tenant_id, TemplateImportCandidate.import_batch_id == batch_id).group_by(TemplateImportCandidate.review_status)).all())
    findings = dict(session.execute(select(CollisionAuditFinding.resolution, func.count()).where(CollisionAuditFinding.tenant_id == tenant_id, CollisionAuditFinding.import_batch_id == batch_id).group_by(CollisionAuditFinding.resolution)).all())
    artifacts = session.execute(select(func.count()).select_from(ImportArtifact).where(ImportArtifact.tenant_id == tenant_id, ImportArtifact.import_batch_id == batch_id)).scalar_one()
    reports = session.execute(select(func.count()).select_from(CorpusReport).where(CorpusReport.tenant_id == tenant_id, CorpusReport.import_batch_id == batch_id)).scalar_one()
    return {**batch_summary(batch), "applied_at": batch.applied_at.isoformat() if batch.applied_at else None, "reverted_at": batch.reverted_at.isoformat() if batch.reverted_at else None, "artifacts": int(artifacts), "corpus_reports": int(reports), "template_candidates": {str(k): int(v) for k, v in candidates.items()}, "collision_findings": {str(k): int(v) for k, v in findings.items()}}


def import_roster_file(session: Session, tenant_id: uuid.UUID, data: bytes, *, trigger: str = ImportTrigger.INITIAL_ONBOARDING) -> dict[str, Any]:
    """Import the lab's roster from an HR CSV export."""
    rows, problems = roster.parse_roster_csv(data)
    if not rows and problems:
        raise StepRefused(422, "; ".join(problems))
    result = roster.import_roster(session, tenant_id=tenant_id, rows=rows, submitted_by=None, trigger=trigger)
    return {"batch_id": str(result.batch.id), "created": len(result.created), "updated": len(result.updated), "profiles_created": len(result.profiles_created), "problems": problems}


def submit_shorthand_files(session: Session, tenant_id: uuid.UUID, uploads: list[ArtifactUpload], *, trigger: str = ImportTrigger.INITIAL_ONBOARDING) -> dict[str, Any]:
    """Read shorthand reference sheets into the lab's lexicon, so speech recognition is biased toward the abbreviations."""
    if not uploads:
        raise StepRefused(422, "choose at least one shorthand reference file")
    result = shorthand.import_shorthand_reference(session, tenant_id=tenant_id, uploads=uploads, trigger=trigger)
    if not result.mappings and result.failures:
        raise StepRefused(422, "; ".join(f"{name}: {reason}" for name, reason in result.failures))
    return {"batch_id": str(result.batch.id), "mappings": len(result.mappings), "terms_created": result.terms_created, "terms_updated": result.terms_updated, "conflicts": result.conflicts, "failures": [{"filename": n, "reason": r} for n, r in result.failures]}


def submit_template_files(session: Session, tenant_id: uuid.UUID, uploads: list[ArtifactUpload], *, trigger: str = ImportTrigger.INITIAL_ONBOARDING) -> dict[str, Any]:
    """Parse uploaded documents into template candidates; nothing goes live."""
    if not uploads:
        raise StepRefused(422, "choose at least one template file")
    result = templates.submit_templates(session, tenant_id=tenant_id, uploads=uploads, submitted_by=None, trigger=trigger)
    return {"batch_id": str(result.batch.id), "candidates": len(result.candidates), "low_confidence": len(result.low_confidence), "failures": [{"filename": name, "reason": reason} for name, reason in result.failures]}


def load_corpus_records(session: Session, tenant_id: uuid.UUID, records: list[corpus.CorpusRecord], *, trigger: str = ImportTrigger.INITIAL_ONBOARDING) -> dict[str, Any]:
    """Bulk-load historical signed reports."""
    result = corpus.load_corpus(session, tenant_id=tenant_id, records=records, submitted_by=None, trigger=trigger)
    return {"batch_id": str(result.batch.id), "loaded": result.loaded, "mapped": result.mapped, "duplicates": result.duplicates, "rejected": [{"id": rid, "reason": reason} for rid, reason in result.rejected]}


def load_corpus_file(session: Session, tenant_id: uuid.UUID, data: bytes, filename: str, *, trigger: str = ImportTrigger.INITIAL_ONBOARDING) -> dict[str, Any]:
    """Bulk-load historical reports from an uploaded CSV or JSON file."""
    records, problems = corpus.parse_corpus_file(data, filename)
    if not records:
        raise StepRefused(422, "; ".join(problems[:5]) or "the file holds no reports")
    result = load_corpus_records(session, tenant_id, records, trigger=trigger)
    return result | {"problems": problems}


def import_gold_bundle_file(session: Session, tenant_id: uuid.UUID, data: bytes, filename: str, *, store: ObjectStore | None = None, trigger: str = ImportTrigger.INITIAL_ONBOARDING) -> dict[str, Any]:
    """File a ZIP of gold bundles: `manifest.csv` plus the audio it names."""
    try:
        bundles, problems = gold_bundles.parse_bundle_zip(data)
    except gold_bundles.BundleFileRefused as exc:
        raise StepRefused(422, str(exc)) from exc
    if not bundles:
        raise StepRefused(422, "; ".join(problems[:5]) or "the manifest lists no bundles")
    result = gold_bundles.import_gold_bundles(session, store or object_store(get_settings().storage), tenant_id=tenant_id, bundles=bundles, upload=ArtifactUpload(filename=filename, data=data, mime_type="application/zip"), trigger=trigger)
    return {"batch_id": str(result.batch.id), "accepted": result.accepted, "reused": result.reused, "per_template": dict(result.per_template), "rejected": [{"id": rid, "reason": reason} for rid, reason in result.rejected], "problems": problems}


def baseline_sample_csv(session: Session, tenant_id: uuid.UUID, class_code: str, *, size: int | None = None) -> str:
    """The reports a grader should re-read next for this class, as a CSV to fill in."""
    try:
        return baseline_audit.export_sample_csv(baseline_audit.draw_sample(session, tenant_id=tenant_id, class_code=class_code, size=size))
    except baseline_audit.BaselineRefused as exc:
        raise StepRefused(404, str(exc)) from exc


def record_baseline_grades(session: Session, tenant_id: uuid.UUID, class_code: str, data: bytes, *, actor_id: uuid.UUID | None = None) -> dict[str, Any]:
    """Store the grades a grader sent back for this class."""
    rows, problems = baseline_audit.parse_grades_csv(data)
    if not rows:
        raise StepRefused(422, "; ".join(problems[:5]) or "the file holds no grades")
    try:
        result = baseline_audit.record_grades(session, tenant_id=tenant_id, class_code=class_code, rows=rows, actor_id=actor_id)
    except baseline_audit.BaselineRefused as exc:
        raise StepRefused(409, str(exc)) from exc
    return {"recorded": result.recorded, "regraded": result.regraded, "rejected": [{"id": rid, "reason": reason} for rid, reason in result.rejected], "problems": problems}


def measure_baseline(session: Session, tenant_id: uuid.UUID, class_code: str, *, actor_id: uuid.UUID | None = None) -> dict[str, Any]:
    """Set the class's baseline error rate from its grades."""
    try:
        klass = baseline_audit.compute_baseline(session, tenant_id=tenant_id, class_code=class_code, actor_id=actor_id)
    except baseline_audit.BaselineRefused as exc:
        raise StepRefused(409, str(exc)) from exc
    graded, cse = baseline_audit.audit_progress(session, tenant_id, klass)
    return {"code": klass.code, "baseline_cse_rate": float(klass.baseline_cse_rate), "graded": graded, "cse": cse}


def propose_template_merges(session: Session, tenant_id: uuid.UUID, batch_id: uuid.UUID) -> dict[str, Any]:
    """Near-duplicate detection on one template batch, before routing is trained."""
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.tenant_id != tenant_id:
        raise StepRefused(404, f"no import batch {batch_id}")
    proposals = templates.propose_merges(session, tenant_id=tenant_id, batch=batch)
    # The count, and each proposal's id: a radiologist decides them one by one.
    return {"proposals": len(proposals), "items": [{"id": str(p.id), "template_a_id": str(p.template_a_id), "template_b_id": str(p.template_b_id), "similarity": float(p.similarity_score) if p.similarity_score is not None else None, "decision": p.decision} for p in proposals]}


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
    """The lab's acceptance set that can still change; once one is frozen, the next version starts empty."""
    sets = list(session.execute(select(EvalSet).where(EvalSet.tenant_id == tenant_id, EvalSet.is_canonical.is_(False)).order_by(EvalSet.created_at.desc())).scalars().all())
    open_set = next((s for s in sets if not s.is_frozen), None)
    if open_set is not None:
        return open_set
    per = gold_per_template(session, tenant_id)
    # A frozen set never changes; a template added later is tested in a new version, which readiness reads once it is frozen.
    created = EvalSet(tenant_id=tenant_id, name=f"acceptance-{tenant_id.hex[:8]}-v{len(sets) + 1}", description="Per-lab acceptance set for the pilot gate: gold bundles for every template. Not a release gate.", is_frozen=False, is_canonical=False, stratification_spec={"per_template": per, "stratify_by": ["template", "radiologist", "audio_quality_bucket"]})
    session.add(created)
    session.flush()
    return created


def _assemble_acceptance(session: Session, tenant_id: uuid.UUID, options: dict[str, Any]) -> dict[str, Any]:
    eval_set = _acceptance_set(session, tenant_id)
    per = gold_per_template(session, tenant_id)
    groups = gold_bundles.candidates_by_template(session, tenant_id)
    goldset.assemble_by_template(session, eval_set=eval_set, groups={version_id: candidates for version_id, _code, candidates in groups.values()}, per_template=per)
    held = gold_bundles.held_per_template(session, tenant_id, eval_set.id)
    return {"eval_set_id": str(eval_set.id), "items": sum(held.values()), "per_template": per, "templates": len(held), "short": {code: per - n for code, n in sorted(held.items()) if n < per}}


def _freeze_acceptance(session: Session, tenant_id: uuid.UUID, options: dict[str, Any]) -> dict[str, Any]:
    eval_set = _acceptance_set(session, tenant_id)
    per = gold_per_template(session, tenant_id)
    held = gold_bundles.held_per_template(session, tenant_id, eval_set.id)
    if not held:
        raise StepRefused(409, "no approved, active templates yet; an acceptance set tests templates")
    short = sorted(code for code, n in held.items() if n < per)
    if short:
        # Frozen means final: a short set would hold this lab's readiness back until a new version is built.
        raise StepRefused(409, f"{len(short)} template(s) have fewer than {per} gold bundles in the set: {', '.join(short[:10])}")
    try:
        goldset.freeze(session, eval_set=eval_set, min_items=per * len(held))
    except ValueError as exc:
        raise StepRefused(409, str(exc)) from exc
    items = session.execute(select(func.count()).select_from(EvalItem).where(EvalItem.eval_set_id == eval_set.id)).scalar_one()
    return {"eval_set_id": str(eval_set.id), "items": items, "frozen": True}


def _annotate_radlex(session: Session, tenant_id: uuid.UUID, options: dict[str, Any]) -> dict[str, Any]:
    """Fill lexicon_term.radlex_id from RadLex for terms that lack one; needs BIOPORTAL_API_KEY."""
    from sqlalchemy import update

    from radreport.db.models.knowledge import LexiconTerm
    from radreport.knowledge.synonyms import RadLexClient

    client = RadLexClient()
    if not client.enabled:
        raise StepRefused(409, "set BIOPORTAL_API_KEY on the server to look terms up in RadLex")
    terms = list(session.execute(select(LexiconTerm.id, LexiconTerm.canonical_form).where(LexiconTerm.tenant_id == tenant_id, LexiconTerm.radlex_id.is_(None)).limit(int(options.get("limit", 200)))).all())
    found = 0
    for term_id, form in terms:
        hit = client.lookup(form)
        if hit is not None and hit.rid:
            session.execute(update(LexiconTerm).where(LexiconTerm.id == term_id).values(radlex_id=hit.rid))
            found += 1
    return {"looked_up": len(terms), "annotated": found}


#: Step name -> (label shown in the panel, what it does), in process order. Each takes optional `options`.
STEPS: dict[str, tuple[str, Callable[[Session, uuid.UUID, dict[str, Any]], dict[str, Any]]]] = {
    "acceptance-assemble": ("Fill the acceptance set from the gold bundles", _assemble_acceptance),
    "acceptance-freeze": ("Freeze the acceptance set", _freeze_acceptance),
    "derive-map": ("Derive the report-to-template map", _derive_map),
    "lexicon-mine": ("Mine terms from the corpus", _mine_lexicon),
    "mine-variants": ("Mine how terms are actually said", _mine_variants),
    "boilerplate-mine": ("Rank normal statements", _mine_boilerplate),
    "radlex-annotate": ("Look terms up in RadLex", _annotate_radlex),
    "collision-audit": ("Run the sound-alike collision audit", _collision_audit),
    "critical-rules-seed": ("Propose critical-finding rules", _seed_critical_rules),
}


def run_step(session: Session, tenant_id: uuid.UUID, step: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one named mining or seeding step."""
    if step not in STEPS:
        raise StepRefused(404, f"no onboarding step {step!r}; choose one of {', '.join(STEPS)}")
    return STEPS[step][1](session, tenant_id, options or {})
