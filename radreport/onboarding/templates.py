"""Turns uploaded reporting templates into the live template library, behind three radiologist sign-offs.

Order: upload documents (submit_templates) -> build each one's field schema
(build_json_schema) -> a radiologist reviews it (review_candidate) -> near-duplicates are
proposed for merging (propose_merges, decide_merge) -> approved templates go live
(apply_templates), and a bad batch is rolled back (revert_applied_templates).
Uploads the parser is unsure of are also read by the lab's template model (onboarding/template_llm.py).
list_pending_review, list_artifacts and model_fields_added feed the review screens.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from radreport.core.errors import ApprovalRequired, BatchBlocked
from radreport.core.logging import get_logger
from radreport.core.types import ActorType, CandidateReviewStatus, FieldDataType, ImportBatchType, ImportStatus, ImportTrigger, MergeDecision, ParseStatus
from radreport.db.models.knowledge import Template, TemplateField, TemplateVersion
from radreport.db.models.onboarding import ImportArtifact, ImportBatch, TemplateImportCandidate, TemplateMergeProposal
from radreport.db.models.orchestration import AuditLog
from radreport.knowledge.phonetics import CollisionCandidate, double_metaphone
from radreport.onboarding import template_llm
from radreport.onboarding.batches import ArtifactUpload, open_batch, record_counts, register_artifact, revert_batch, transition
from radreport.onboarding.lexicon import pending_blocking_collisions, run_collision_audit
from radreport.onboarding.template_parse import ParsedTemplate, UnsupportedDocument, _title_from_filename, extract_paragraphs, infer_structure

log = get_logger(__name__)

#: Below this, the reviewing radiologist gets a mandatory field-by-field pass
#: rather than a summary card.
LOW_CONFIDENCE_THRESHOLD = 0.55

#: Jaccard overlap of field keys above which two templates are proposed as duplicates.
MERGE_SIMILARITY_THRESHOLD = 0.80


@dataclass(slots=True)
class TemplateSubmission:
    batch: ImportBatch
    candidates: list[TemplateImportCandidate] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    """`(filename, reason)` — surfaced to the lab admin, not swallowed."""

    low_confidence: list[TemplateImportCandidate] = field(default_factory=list)
    model_assisted: list[str] = field(default_factory=list)
    """Filenames the template model also read."""


def submit_templates(session: Session, *, tenant_id: uuid.UUID, uploads: list[ArtifactUpload], submitted_by: uuid.UUID | None = None, trigger: str = ImportTrigger.INITIAL_ONBOARDING, batch: ImportBatch | None = None, fallback: template_llm.Fallback | None = None) -> TemplateSubmission:
    """Parse uploaded documents into candidates; an unsure parse is also read by the lab's template model. Nothing goes live here."""
    batch = batch or open_batch(session, tenant_id=tenant_id, batch_type=ImportBatchType.TEMPLATE, stage="S1", trigger=trigger, submitted_by=submitted_by)
    if batch.status == ImportStatus.UPLOADING:
        transition(session, batch, ImportStatus.PARSING, actor_id=submitted_by)

    submission = TemplateSubmission(batch=batch)
    model, looked_up = fallback, fallback is not None

    for upload in uploads:
        artifact, created = register_artifact(session, batch, upload)
        if not created:
            # Idempotent by content hash: the same document twice in one batch
            # is one candidate.
            continue

        try:
            paragraphs = extract_paragraphs(upload.data, upload.filename)
            parsed = infer_structure(paragraphs, fallback_title=_title_from_filename(upload.filename))
        except UnsupportedDocument as exc:
            artifact.parse_status = ParseStatus.FAILED
            artifact.parse_warnings = {"error": str(exc)}
            submission.failures.append((upload.filename, str(exc)))
            record_counts(session, batch, rejected=1)
            continue

        regex_confidence = parsed.confidence
        outcome: template_llm.FallbackOutcome | None = None
        if template_llm.needs_fallback(session, tenant_id, parsed):
            if not looked_up:
                model, looked_up = template_llm.fallback_for(session, tenant_id), True
            if model is not None:
                outcome = template_llm.apply_fallback(paragraphs, parsed, model)
                parsed = outcome.parsed
                submission.model_assisted.append(upload.filename)

        artifact.parse_status = ParseStatus.OK if parsed.fields else ParseStatus.PARTIAL
        warnings: dict[str, Any] = {"warnings": parsed.warnings} if parsed.warnings else {}
        if outcome is not None:
            warnings["template_model"] = {**outcome.audit(), "parser_confidence": regex_confidence}
        artifact.parse_warnings = warnings or None

        candidate = TemplateImportCandidate(tenant_id=tenant_id, import_batch_id=batch.id, import_artifact_id=artifact.id, proposed_code=_propose_code(parsed), proposed_json_schema=build_json_schema(parsed), proposed_sections={"sections": parsed.sections}, proposed_modality=parsed.modality, proposed_body_region=parsed.body_region, proposed_spoken_study_code=_propose_spoken_code(parsed), parse_confidence=parsed.confidence, review_status=CandidateReviewStatus.PENDING, source_text=template_llm.document_text(paragraphs))
        session.add(candidate)
        session.flush()
        submission.candidates.append(candidate)
        if parsed.confidence < LOW_CONFIDENCE_THRESHOLD:
            submission.low_confidence.append(candidate)

    session.flush()
    if batch.status == ImportStatus.PARSING:
        transition(session, batch, ImportStatus.AWAITING_REVIEW, actor_id=submitted_by)

    log.info("s1_templates_submitted", tenant_id=str(tenant_id), batch_id=str(batch.id), candidates=len(submission.candidates), low_confidence=len(submission.low_confidence), model_assisted=len(submission.model_assisted), failures=len(submission.failures))
    return submission


def build_json_schema(parsed: ParsedTemplate) -> dict[str, Any]:
    """A real JSON Schema — it drives constrained decoding."""
    properties: dict[str, Any] = {}
    for f in parsed.fields:
        prop: dict[str, Any] = {"title": f.display_label, "x-section": f.section, "x-seq": f.seq, "x-data-type": f.data_type}
        if f.data_type == FieldDataType.ENUM:
            prop["type"] = "string"
            prop["enum"] = list(f.enum_values)
        elif f.data_type == FieldDataType.MEASUREMENT:
            prop["type"] = "number"
            prop["x-unit"] = f.unit
        else:
            prop["type"] = "string"
        properties[f.field_key] = prop

    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": parsed.title, "type": "object", "properties": properties, "required": [], "additionalProperties": False}


def _propose_code(parsed: ParsedTemplate) -> str:
    parts = [p for p in (parsed.modality, parsed.body_region) if p]
    if parts:
        return "_".join(p.upper() for p in parts)
    return parsed.title.upper().replace(" ", "_")[:40]


def _propose_spoken_code(parsed: ParsedTemplate) -> str:
    """A first guess at what the radiologist will say. **Always reviewed.**"""
    return parsed.title.strip().lower()


# --------------------------------------------------------------- review -----
def review_candidate(session: Session, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID, reviewer_id: uuid.UUID, spoken_study_code: str | None = None, code: str | None = None, modality: str | None = None, body_region: str | None = None, json_schema: dict[str, Any] | None = None, decision: str = CandidateReviewStatus.APPROVED) -> TemplateImportCandidate:
    """The per-schema and per-code gate, as one recorded act."""
    candidate = session.get(TemplateImportCandidate, candidate_id)
    if candidate is None or candidate.tenant_id != tenant_id:
        raise ValueError(f"no template_import_candidate {candidate_id} in this tenant")
    if decision not in CandidateReviewStatus.values():
        raise ValueError(f"unknown review decision {decision!r}")

    before = {"review_status": candidate.review_status, "proposed_spoken_study_code": candidate.proposed_spoken_study_code, "proposed_code": candidate.proposed_code}
    edited = False
    if spoken_study_code is not None and spoken_study_code != candidate.proposed_spoken_study_code:
        candidate.proposed_spoken_study_code = spoken_study_code.strip()
        edited = True
    if code is not None and code != candidate.proposed_code:
        candidate.proposed_code = code.strip()
        edited = True
    if modality is not None:
        candidate.proposed_modality = modality
    if body_region is not None:
        candidate.proposed_body_region = body_region
    if json_schema is not None:
        candidate.proposed_json_schema = json_schema
        edited = True

    if decision == CandidateReviewStatus.APPROVED and edited:
        decision = CandidateReviewStatus.EDITED
    candidate.review_status = decision

    if decision in (CandidateReviewStatus.APPROVED, CandidateReviewStatus.EDITED):
        if not candidate.proposed_spoken_study_code:
            raise ValueError("a spoken_study_code is required before approval: it is the primary routing anchor and the collision audit's input")

    session.add(AuditLog(tenant_id=tenant_id, actor_id=reviewer_id, actor_type=ActorType.USER, action="template_candidate_reviewed", entity_type="template_import_candidate", entity_id=candidate.id, before=before, after={"review_status": candidate.review_status, "proposed_spoken_study_code": candidate.proposed_spoken_study_code, "proposed_code": candidate.proposed_code}))
    session.flush()
    return candidate


# ---------------------------------------------------------------- merges ----
def propose_merges(session: Session, *, tenant_id: uuid.UUID, batch: ImportBatch) -> list[TemplateMergeProposal]:
    """Flag near-duplicates, both candidate→live and live→live."""
    candidates = list(session.execute(select(TemplateImportCandidate).where(TemplateImportCandidate.tenant_id == tenant_id, TemplateImportCandidate.import_batch_id == batch.id, TemplateImportCandidate.review_status == CandidateReviewStatus.PENDING)).scalars().all())

    live = list(session.execute(select(Template, TemplateVersion).join(TemplateVersion, TemplateVersion.template_id == Template.id).where(Template.tenant_id == tenant_id, TemplateVersion.is_current.is_(True))).all())

    for candidate in candidates:
        candidate_keys = _schema_field_keys(candidate.proposed_json_schema)
        best: tuple[float, Template] | None = None
        for template, version in live:
            score = _similarity(candidate_keys, _schema_field_keys(version.json_schema))
            if score >= MERGE_SIMILARITY_THRESHOLD and (best is None or score > best[0]):
                best = (score, template)
        if best is not None:
            candidate.merged_into_template_id = best[1].id
            log.info("s1_merge_suggested", tenant_id=str(tenant_id), candidate_id=str(candidate.id), template_id=str(best[1].id), similarity=round(best[0], 4))

    proposals: list[TemplateMergeProposal] = []
    existing = {tuple(sorted((str(a), str(b)))) for a, b in session.execute(select(TemplateMergeProposal.template_a_id, TemplateMergeProposal.template_b_id).where(TemplateMergeProposal.tenant_id == tenant_id)).all()}

    for i, (template_a, version_a) in enumerate(live):
        for template_b, version_b in live[i + 1 :]:
            key = tuple(sorted((str(template_a.id), str(template_b.id))))
            if key in existing:
                continue
            keys_a = _schema_field_keys(version_a.json_schema)
            keys_b = _schema_field_keys(version_b.json_schema)
            score = _similarity(keys_a, keys_b)
            if score < MERGE_SIMILARITY_THRESHOLD:
                continue
            proposal = TemplateMergeProposal(tenant_id=tenant_id, import_batch_id=batch.id, template_a_id=template_a.id, template_b_id=template_b.id, similarity_score=round(score, 4), differing_fields={"only_in_a": sorted(keys_a - keys_b), "only_in_b": sorted(keys_b - keys_a)}, decision=MergeDecision.PENDING)
            session.add(proposal)
            proposals.append(proposal)
            existing.add(key)

    session.flush()
    return proposals


def _schema_field_keys(schema: dict[str, Any] | None) -> frozenset[str]:
    if not schema:
        return frozenset()
    return frozenset((schema.get("properties") or {}).keys())


def _similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard overlap of field keys."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def decide_merge(session: Session, *, tenant_id: uuid.UUID, proposal_id: uuid.UUID, decision: str, decided_by: uuid.UUID) -> TemplateMergeProposal:
    """The per-merge gate. Radiologist only."""
    proposal = session.get(TemplateMergeProposal, proposal_id)
    if proposal is None or proposal.tenant_id != tenant_id:
        raise ValueError(f"no template_merge_proposal {proposal_id} in this tenant")
    if decision not in MergeDecision.values():
        raise ValueError(f"unknown merge decision {decision!r}")

    previous = proposal.decision
    proposal.decision = decision
    proposal.decided_by = decided_by

    if decision == MergeDecision.MERGE:
        # Deactivate B and let A carry the volume.
        template_b = session.get(Template, proposal.template_b_id)
        if template_b is not None and template_b.tenant_id == tenant_id:
            template_b.is_active = False

    session.add(AuditLog(tenant_id=tenant_id, actor_id=decided_by, actor_type=ActorType.USER, action="template_merge_decided", entity_type="template_merge_proposal", entity_id=proposal.id, before={"decision": previous}, after={"decision": decision, "template_a_id": str(proposal.template_a_id), "template_b_id": str(proposal.template_b_id)}))
    session.flush()
    log.info("s1_merge_decided", tenant_id=str(tenant_id), proposal_id=str(proposal.id), decision=decision)
    return proposal


# ----------------------------------------------------------------- apply ----
@dataclass(slots=True)
class ApplyResult:
    batch: ImportBatch
    templates_created: list[Template] = field(default_factory=list)
    versions_created: list[TemplateVersion] = field(default_factory=list)
    skipped: list[tuple[uuid.UUID, str]] = field(default_factory=list)


def apply_templates(session: Session, *, tenant_id: uuid.UUID, batch: ImportBatch, approver_id: uuid.UUID) -> ApplyResult:
    """Promote approved candidates into `template_version`."""
    candidates = list(session.execute(select(TemplateImportCandidate).where(TemplateImportCandidate.tenant_id == tenant_id, TemplateImportCandidate.import_batch_id == batch.id, TemplateImportCandidate.review_status.in_((CandidateReviewStatus.APPROVED, CandidateReviewStatus.EDITED)))).scalars().all())
    if not candidates:
        raise ApprovalRequired(f"import_batch {batch.id} has no approved candidates; a radiologist must approve each schema and each spoken_study_code first ")

    # The codes this batch would introduce, audited against everything already
    # live before a single row is written.
    incoming = [CollisionCandidate(label=c.proposed_spoken_study_code or "", maps_to=c.proposed_code) for c in candidates if c.proposed_spoken_study_code]
    run_collision_audit(session, tenant_id=tenant_id, batch=batch, extra_candidates=incoming)
    # Counted by label, not by batch: the audit records a pair once, so a collision found earlier
    # (by the lab-wide audit or another batch) would otherwise not hold this batch back.
    batch.blocking_issue_count = pending_blocking_collisions(session, tenant_id=tenant_id, labels={c.label for c in incoming}, batch_id=batch.id)
    session.flush()
    if batch.blocking_issue_count:
        raise BatchBlocked(str(batch.id), batch.blocking_issue_count)

    result = ApplyResult(batch=batch)
    now = dt.datetime.now(dt.UTC)

    for candidate in candidates:
        template = _resolve_template(session, tenant_id, candidate, result)
        if template is None:
            continue

        version = _create_version(session, tenant_id=tenant_id, template=template, candidate=candidate, approver_id=approver_id, effective_from=now)
        result.versions_created.append(version)
        candidate.promoted_template_version_id = version.id

    record_counts(session, batch, accepted=len(result.versions_created))

    if batch.status == ImportStatus.AWAITING_REVIEW:
        transition(session, batch, ImportStatus.APPROVED, actor_id=approver_id)
    transition(session, batch, ImportStatus.APPLIED, actor_id=approver_id)

    log.info("s1_templates_applied", tenant_id=str(tenant_id), batch_id=str(batch.id), templates_created=len(result.templates_created), versions_created=len(result.versions_created), skipped=len(result.skipped))
    return result


def _resolve_template(session: Session, tenant_id: uuid.UUID, candidate: TemplateImportCandidate, result: ApplyResult) -> Template | None:
    """Find or create the `template` a candidate belongs to."""
    if candidate.merged_into_template_id is not None:
        template = session.get(Template, candidate.merged_into_template_id)
        if template is not None and template.tenant_id == tenant_id:
            return template

    code = candidate.proposed_code
    if not code:
        result.skipped.append((candidate.id, "no template code"))
        return None

    existing = session.execute(select(Template).where(Template.tenant_id == tenant_id, Template.code == code)).scalar_one_or_none()
    if existing is not None:
        return existing

    schema = candidate.proposed_json_schema or {}
    template = Template(tenant_id=tenant_id, code=code, display_name=schema.get("title") or code, modality=candidate.proposed_modality or "UNKNOWN", body_region=candidate.proposed_body_region or "unspecified")
    session.add(template)
    session.flush()
    result.templates_created.append(template)
    return template


def _create_version(session: Session, *, tenant_id: uuid.UUID, template: Template, candidate: TemplateImportCandidate, approver_id: uuid.UUID, effective_from: dt.datetime) -> TemplateVersion:
    """Write a new immutable version and its fields, and make it current."""
    previous = list(session.execute(select(TemplateVersion).where(TemplateVersion.tenant_id == tenant_id, TemplateVersion.template_id == template.id).order_by(TemplateVersion.version.desc())).scalars().all())
    for old in previous:
        old.is_current = False

    next_version = (previous[0].version + 1) if previous else 1
    schema = candidate.proposed_json_schema or {}
    sections = (candidate.proposed_sections or {}).get("sections", [])
    spoken = (candidate.proposed_spoken_study_code or "").strip()
    primary, _secondary = double_metaphone(spoken)

    version = TemplateVersion(tenant_id=tenant_id, template_id=template.id, version=next_version, json_schema=schema, render_spec={"sections": sections, "field_order": list(schema.get("properties", {}))}, routing_card=_routing_card(template, schema, sections), trigger_rules={"spoken_study_code": spoken, "modality": template.modality, "body_region": template.body_region}, study_description_patterns=None, spoken_study_code=spoken, spoken_study_code_phonetic=primary, approved_by=approver_id, approved_at=dt.datetime.now(dt.UTC), effective_from=effective_from, is_current=True)
    session.add(version)
    session.flush()

    for key, prop in (schema.get("properties") or {}).items():
        session.add(
            TemplateField(
                tenant_id=tenant_id,
                template_version_id=version.id,
                field_key=key,
                section=prop.get("x-section") or "FINDINGS",
                display_label=prop.get("title") or key,
                data_type=prop.get("x-data-type") or FieldDataType.TEXT,
                enum_values=prop.get("enum"),
                unit=prop.get("x-unit"),
                is_required=False,
                # , by construction: every field starts `leave_blank_flag` (the Pass 1 promotes `is_critical`).
                absence_policy="leave_blank_flag",
                seq=prop.get("x-seq") or 0,
            )
        )
    session.flush()
    return version


def _routing_card(template: Template, schema: dict[str, Any], sections: list[str]) -> str:
    """The prose the router embeds and matches against."""
    title = schema.get("title") or template.display_name
    parts = [f"{title}.", f"Modality {template.modality}, region {template.body_region}."]
    if sections:
        parts.append(f"Sections: {', '.join(sections)}.")
    labels = [prop.get("title", key) for key, prop in list((schema.get("properties") or {}).items())[:12]]
    if labels:
        parts.append(f"Reports on: {', '.join(labels)}.")
    return " ".join(parts)


def revert_applied_templates(session: Session, *, tenant_id: uuid.UUID, batch: ImportBatch, actor_id: uuid.UUID | None = None) -> list[TemplateVersion]:
    """The rollback: re-point `is_current` at the previous version, and mark the batch reverted."""
    # First, so a batch that was never applied, or was already reverted, is refused before anything moves.
    revert_batch(session, batch, actor_id=actor_id)
    promoted = list(session.execute(select(TemplateVersion).join(TemplateImportCandidate, TemplateImportCandidate.promoted_template_version_id == TemplateVersion.id).where(TemplateImportCandidate.tenant_id == tenant_id, TemplateImportCandidate.import_batch_id == batch.id)).scalars().all())

    restored: list[TemplateVersion] = []
    for version in promoted:
        version.is_current = False
        prior = session.execute(select(TemplateVersion).where(TemplateVersion.tenant_id == tenant_id, TemplateVersion.template_id == version.template_id, TemplateVersion.version < version.version).order_by(TemplateVersion.version.desc())).scalars().first()
        if prior is not None:
            prior.is_current = True
            restored.append(prior)

        session.add(AuditLog(tenant_id=tenant_id, actor_id=actor_id, actor_type=ActorType.USER if actor_id else ActorType.SYSTEM, action="template_version_reverted", entity_type="template_version", entity_id=version.id, before={"is_current": True, "version": version.version}, after={"is_current": False, "restored_version": prior.version if prior else None}))

    session.flush()
    log.warning("s1_templates_reverted", tenant_id=str(tenant_id), batch_id=str(batch.id), reverted=len(promoted), restored=len(restored))
    return restored


def pending_review_query(*, tenant_id: uuid.UUID, batch_id: uuid.UUID | None = None) -> Select[tuple[TemplateImportCandidate]]:
    """The radiologists' review queue, low-confidence parses first, sorted in the database so it can be paged."""
    stmt = select(TemplateImportCandidate).where(TemplateImportCandidate.tenant_id == tenant_id, TemplateImportCandidate.review_status == CandidateReviewStatus.PENDING)
    if batch_id is not None:
        stmt = stmt.where(TemplateImportCandidate.import_batch_id == batch_id)
    return stmt.order_by(TemplateImportCandidate.parse_confidence.asc().nulls_last(), TemplateImportCandidate.id)


def list_pending_review(session: Session, *, tenant_id: uuid.UUID, batch_id: uuid.UUID | None = None) -> list[TemplateImportCandidate]:
    """The radiologists' review queue, low-confidence parses first."""
    return list(session.execute(pending_review_query(tenant_id=tenant_id, batch_id=batch_id)).scalars().all())


def model_fields_added(session: Session, artifact_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Per artifact, how many fields the template model added; one query for a page of candidates."""
    if not artifact_ids:
        return {}
    rows = session.execute(select(ImportArtifact.id, ImportArtifact.parse_warnings).where(ImportArtifact.id.in_(artifact_ids))).all()
    return {artifact_id: int(((warnings or {}).get("template_model") or {}).get("fields_added") or 0) for artifact_id, warnings in rows}


def list_artifacts(session: Session, *, tenant_id: uuid.UUID, batch_id: uuid.UUID) -> list[ImportArtifact]:
    return list(session.execute(select(ImportArtifact).where(ImportArtifact.tenant_id == tenant_id, ImportArtifact.import_batch_id == batch_id)).scalars().all())
