"""Mines a lab's own vocabulary out of its reports, then audits the terms that sound alike.

Order: mine candidate terms (mine_terms, run_mining) -> attach them to this lab's lexicon
(get_or_create_tenant_lexicon, record_surface_variants) -> find terms that collide
phonetically (collect_audit_candidates, run_collision_audit) -> a radiologist settles each
collision (resolve_finding).
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.text import split_sentences
from radreport.core.types import ActorType, CollisionResolution, CollisionSeverity, ImportBatchType, ImportStatus, ImportTrigger, LexiconScope, TermType, VariantSource
from radreport.db.models.knowledge import LexiconSet, LexiconSurfaceVariant, LexiconTerm, Template, TemplateVersion
from radreport.db.models.onboarding import CollisionAuditFinding, CorpusReport, ImportBatch, LexiconMiningRun
from radreport.db.models.orchestration import AuditLog
from radreport.knowledge.phonetics import CollisionCandidate, audit_collisions, double_metaphone
from radreport.onboarding.batches import open_batch, transition

log = get_logger(__name__)

#: A spelled acronym as it appears in a signed report: 2–5 capitals.
_ACRONYM = re.compile(r"\b([A-Z]{2,5})\b")
#: Multi-word anatomical/pathological phrases worth carrying as bias terms.
_PHRASE = re.compile(r"\b([a-z]+(?:\s+[a-z]+){1,2})\b")

#: Below this many occurrences a mined term is noise, not vocabulary — tuned for the ~5–10k report corpora assumes.
DEFAULT_MIN_FREQUENCY = 5

#: Terms above this share of reports are stopword-like and carry no signal.
MAX_DOCUMENT_SHARE = 0.60


@dataclass(frozen=True, slots=True)
class MinedTerm:
    canonical_form: str
    term_type: str
    frequency: int
    document_count: int
    contexts: tuple[str, ...] = ()
    """Up to three sentences the term appeared in."""


@dataclass(slots=True)
class MiningResult:
    run: LexiconMiningRun
    lexicon_set: LexiconSet
    terms_extracted: int = 0
    terms_new: int = 0
    terms_pending_review: int = 0
    ambiguous: list[str] = field(default_factory=list)


def mine_terms(texts: list[str], *, min_frequency: int = DEFAULT_MIN_FREQUENCY, known_terms: frozenset[str] = frozenset()) -> list[MinedTerm]:
    """Extract candidate vocabulary from report text."""
    acronym_counts: Counter[str] = Counter()
    acronym_docs: Counter[str] = Counter()
    phrase_counts: Counter[str] = Counter()
    phrase_docs: Counter[str] = Counter()
    contexts: dict[str, list[str]] = {}

    for text in texts:
        seen_acronyms: set[str] = set()
        seen_phrases: set[str] = set()
        for sentence in split_sentences(text):
            stripped = sentence.strip()
            if not stripped:
                continue
            for match in _ACRONYM.finditer(stripped):
                token = match.group(1)
                acronym_counts[token] += 1
                seen_acronyms.add(token)
                bucket = contexts.setdefault(token, [])
                if len(bucket) < 3 and stripped not in bucket:
                    bucket.append(stripped)
            for match in _PHRASE.finditer(stripped.lower()):
                phrase = match.group(1)
                if _is_stopword_phrase(phrase):
                    continue
                phrase_counts[phrase] += 1
                seen_phrases.add(phrase)

        for token in seen_acronyms:
            acronym_docs[token] += 1
        for phrase in seen_phrases:
            phrase_docs[phrase] += 1

    total_docs = max(1, len(texts))
    mined: list[MinedTerm] = []

    for token, count in acronym_counts.items():
        if count < min_frequency or token.lower() in known_terms:
            continue
        mined.append(MinedTerm(canonical_form=token, term_type=TermType.ABBREVIATION, frequency=count, document_count=acronym_docs[token], contexts=tuple(contexts.get(token, ()))))

    for phrase, count in phrase_counts.items():
        if count < min_frequency or phrase in known_terms:
            continue
        if phrase_docs[phrase] / total_docs > MAX_DOCUMENT_SHARE:
            continue
        mined.append(MinedTerm(canonical_form=phrase, term_type=TermType.ANATOMY, frequency=count, document_count=phrase_docs[phrase]))

    return sorted(mined, key=lambda t: (-t.frequency, t.canonical_form))


_STOPWORDS = frozenset({"the", "and", "is", "are", "was", "were", "no", "not", "with", "without", "there", "this", "that", "in", "of", "to", "for", "on", "at", "as", "be", "seen", "noted", "shows", "appears", "within", "normal limits", "evidence"})


def _is_stopword_phrase(phrase: str) -> bool:
    words = phrase.split()
    return all(word in _STOPWORDS for word in words)


def run_mining(session: Session, *, tenant_id: uuid.UUID, lexicon_set: LexiconSet | None = None, min_frequency: int = DEFAULT_MIN_FREQUENCY, submitted_by: uuid.UUID | None = None, batch: ImportBatch | None = None, trigger: str = ImportTrigger.INITIAL_ONBOARDING) -> MiningResult:
    """Mine this tenant's corpus into its lexicon set. Safe to re-run."""
    batch = batch or open_batch(session, tenant_id=tenant_id, batch_type=ImportBatchType.SHORTHAND, stage="S3", trigger=trigger, submitted_by=submitted_by)
    if batch.status == ImportStatus.UPLOADING:
        transition(session, batch, ImportStatus.PARSING, actor_id=submitted_by)

    lexicon_set = lexicon_set or get_or_create_tenant_lexicon(session, tenant_id)

    texts = list(session.execute(select(CorpusReport.report_text).where(CorpusReport.tenant_id == tenant_id)).scalars().all())

    known = frozenset(term.lower() for term in session.execute(select(LexiconTerm.canonical_form).where(LexiconTerm.lexicon_set_id.is_(None))).scalars().all())
    mined = mine_terms(texts, min_frequency=min_frequency, known_terms=known)

    pass_number = session.execute(select(func.count()).select_from(LexiconMiningRun).where(LexiconMiningRun.tenant_id == tenant_id)).scalar_one() + 1

    result = MiningResult(run=LexiconMiningRun(tenant_id=tenant_id, import_batch_id=batch.id, corpus_size=len(texts), terms_extracted=len(mined), pass_number=pass_number), lexicon_set=lexicon_set, terms_extracted=len(mined))

    for term in mined:
        existing = session.execute(select(LexiconTerm).where(LexiconTerm.lexicon_set_id == lexicon_set.id, LexiconTerm.canonical_form == term.canonical_form)).scalar_one_or_none()
        if existing is not None:
            # Re-running term mining refreshes the ranking and leaves curation alone.
            existing.frequency_rank = term.frequency
            continue

        primary, secondary = double_metaphone(term.canonical_form)
        ambiguous = _is_polysemous(term)
        session.add(LexiconTerm(tenant_id=tenant_id, lexicon_set_id=lexicon_set.id, canonical_form=term.canonical_form, term_type=term.term_type, phonetic_key_primary=primary, phonetic_key_secondary=secondary, frequency_rank=term.frequency, is_ambiguous=ambiguous))
        result.terms_new += 1
        if ambiguous:
            result.ambiguous.append(term.canonical_form)

    # Every new term is pending review: the term mining stage's human gate is polysemy review, and auto-accepting on frequency alone is what puts PA into the bias list with one meaning attached.
    result.terms_pending_review = result.terms_new
    result.run.terms_new = result.terms_new
    result.run.terms_auto_accepted = 0
    result.run.terms_pending_review = result.terms_pending_review
    session.add(result.run)
    session.flush()

    log.info("s3_mining_complete", tenant_id=str(tenant_id), batch_id=str(batch.id), pass_number=pass_number, corpus_size=len(texts), terms_extracted=result.terms_extracted, terms_new=result.terms_new, ambiguous=len(result.ambiguous))
    return result


#: flags these as needing context resolution.
KNOWN_POLYSEMOUS: frozenset[str] = frozenset({"PA", "RA", "CA", "AP", "LA", "MR", "AS", "PE"})


def _is_polysemous(term: MinedTerm) -> bool:
    return term.canonical_form.upper() in KNOWN_POLYSEMOUS


def get_or_create_tenant_lexicon(session: Session, tenant_id: uuid.UUID) -> LexiconSet:
    """This lab's own set. The global (NULL-tenant) set is what it mines against."""
    existing = session.execute(select(LexiconSet).where(LexiconSet.tenant_id == tenant_id, LexiconSet.scope == LexiconScope.GLOBAL).order_by(LexiconSet.version.desc())).scalars().first()
    if existing is not None:
        return existing

    lexicon_set = LexiconSet(tenant_id=tenant_id, name="tenant-mined", version=1, scope=LexiconScope.GLOBAL, is_active=True)
    session.add(lexicon_set)
    session.flush()
    return lexicon_set


def record_surface_variants(session: Session, *, tenant_id: uuid.UUID, term_id: uuid.UUID, variants: dict[str, int], source: str = VariantSource.MINED) -> int:
    """Record how a term actually comes back from ASR."""
    written = 0
    for raw_surface, count in variants.items():
        surface = raw_surface.strip()
        if not surface:
            continue
        existing = session.execute(select(LexiconSurfaceVariant).where(LexiconSurfaceVariant.lexicon_term_id == term_id, LexiconSurfaceVariant.surface_text == surface)).scalar_one_or_none()
        if existing is not None:
            existing.observed_count += count
            continue
        primary, _ = double_metaphone(surface)
        session.add(LexiconSurfaceVariant(tenant_id=tenant_id, lexicon_term_id=term_id, surface_text=surface, phonetic_key=primary, observed_count=count, source=source))
        written += 1
    session.flush()
    return written


# ------------------------------------------------------- collision audit ----
def collect_audit_candidates(session: Session, tenant_id: uuid.UUID) -> list[CollisionCandidate]:
    """Every string a radiologist might *say* that the system must tell apart."""
    candidates: list[CollisionCandidate] = []

    rows = session.execute(select(TemplateVersion.spoken_study_code, Template.code).join(Template, Template.id == TemplateVersion.template_id).where(TemplateVersion.tenant_id == tenant_id, TemplateVersion.is_current.is_(True))).all()
    for spoken_code, template_code in rows:
        candidates.append(CollisionCandidate(label=spoken_code, maps_to=template_code))

    terms = session.execute(select(LexiconTerm).join(LexiconSet, LexiconSet.id == LexiconTerm.lexicon_set_id).where(LexiconSet.tenant_id == tenant_id, LexiconTerm.term_type.in_((TermType.CODE_WORD, TermType.ABBREVIATION)))).scalars().all()
    for term in terms:
        candidates.append(
            CollisionCandidate(
                label=term.short_form or term.canonical_form,
                # A term's meaning *is* its target. Two abbreviations expanding
                # to the same canonical form are a duplicate, not a collision.
                maps_to=term.canonical_form,
                term_id=str(term.id),
            )
        )

    return candidates


def run_collision_audit(session: Session, *, tenant_id: uuid.UUID, batch: ImportBatch | None = None, extra_candidates: list[CollisionCandidate] | None = None) -> list[CollisionAuditFinding]:
    """Audit every spoken label pairwise and persist the findings."""
    candidates = collect_audit_candidates(session, tenant_id)
    if extra_candidates:
        candidates.extend(extra_candidates)

    findings = audit_collisions(candidates)
    existing_pairs = {_pair_key(a, b) for a, b in session.execute(select(CollisionAuditFinding.label_a, CollisionAuditFinding.label_b).where(CollisionAuditFinding.tenant_id == tenant_id)).all()}

    written: list[CollisionAuditFinding] = []
    for finding in findings:
        key = _pair_key(finding.a.label, finding.b.label)
        if key in existing_pairs:
            continue
        row = CollisionAuditFinding(tenant_id=tenant_id, import_batch_id=batch.id if batch else None, term_a_id=uuid.UUID(finding.a.term_id) if finding.a.term_id else None, term_b_id=uuid.UUID(finding.b.term_id) if finding.b.term_id else None, label_a=finding.a.label, label_b=finding.b.label, phonetic_distance=finding.distance, collision_class=finding.collision_class, severity=finding.severity, resolution=CollisionResolution.PENDING)
        session.add(row)
        written.append(row)
        existing_pairs.add(key)

    session.flush()
    blocking = sum(1 for f in written if f.severity == CollisionSeverity.BLOCK)
    if batch is not None:
        batch.blocking_issue_count = session.execute(select(func.count()).select_from(CollisionAuditFinding).where(CollisionAuditFinding.tenant_id == tenant_id, CollisionAuditFinding.import_batch_id == batch.id, CollisionAuditFinding.severity == CollisionSeverity.BLOCK, CollisionAuditFinding.resolution == CollisionResolution.PENDING)).scalar_one()
        session.flush()

    log.info("s3_collision_audit_complete", tenant_id=str(tenant_id), candidates=len(candidates), new_findings=len(written), new_blocking=blocking)
    return written


def pending_blocking_collisions(session: Session, *, tenant_id: uuid.UUID, labels: set[str], batch_id: uuid.UUID | None = None) -> int:
    """Unresolved block-severity findings touching any of `labels` (or filed under `batch_id`), whoever recorded them."""
    lowered = {label.lower() for label in labels if label}
    involved = or_(func.lower(CollisionAuditFinding.label_a).in_(lowered), func.lower(CollisionAuditFinding.label_b).in_(lowered))
    if batch_id is not None:
        involved = or_(involved, CollisionAuditFinding.import_batch_id == batch_id)
    return session.execute(select(func.count()).select_from(CollisionAuditFinding).where(CollisionAuditFinding.tenant_id == tenant_id, CollisionAuditFinding.severity == CollisionSeverity.BLOCK, CollisionAuditFinding.resolution == CollisionResolution.PENDING, involved)).scalar_one()


def _pair_key(a: str, b: str) -> tuple[str, str]:
    """Order-independent identity for a finding."""
    return (a, b) if a <= b else (b, a)


def resolve_finding(session: Session, *, tenant_id: uuid.UUID, finding_id: uuid.UUID, resolution: str, resolved_by: uuid.UUID) -> CollisionAuditFinding:
    """Close a collision finding. Radiologist decision, recorded as one."""
    finding = session.get(CollisionAuditFinding, finding_id)
    if finding is None or finding.tenant_id != tenant_id:
        raise ValueError(f"no collision_audit_finding {finding_id} in this tenant")
    if resolution not in CollisionResolution.values():
        raise ValueError(f"unknown resolution {resolution!r}")

    previous = finding.resolution
    finding.resolution = resolution
    finding.resolved_by = resolved_by

    session.add(AuditLog(tenant_id=tenant_id, actor_id=resolved_by, actor_type=ActorType.USER, action="collision_finding_resolved", entity_type="collision_audit_finding", entity_id=finding.id, before={"resolution": previous}, after={"resolution": resolution, "label_a": finding.label_a, "label_b": finding.label_b, "severity": finding.severity}))
    session.flush()
    log.info("s3_collision_resolved", tenant_id=str(tenant_id), finding_id=str(finding.id), resolution=resolution, severity=finding.severity)
    return finding
