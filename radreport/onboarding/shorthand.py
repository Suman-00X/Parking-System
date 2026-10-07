"""Reads a lab's shorthand reference (the sheet transcriptionists keep) into lexicon terms, so speech recognition is biased toward the abbreviations radiologists actually say.

Order: pull text out of a PDF, Word or text file (extract_text) -> find `short -> formal` pairs with
eight patterns (find_mappings: "LLL = Left Lower Lobe", "RLL -> Right lower lobe", "PNA | Pneumonia",
"CBD: common bile duct", "IVC - inferior vena cava", "left lower lobe (LLL)", two-column tables,
and slash groups such as "RUL/LUL/RLL/LLL" expanded from the pairs around them) -> score each pair
by how well the short form spells the formal one (acronym_fit) -> write them into the lab's lexicon,
each with an audit entry naming the file and line it came from (import_shorthand_reference).
The terms reach the speech engine through build_keyterms, which already biases toward short forms.
"""

from __future__ import annotations

import io
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.types import ActorType, ImportBatchType, ImportStatus, ImportTrigger, TermType
from radreport.db.models.knowledge import LexiconTerm
from radreport.db.models.onboarding import ImportBatch
from radreport.db.models.orchestration import AuditLog
from radreport.knowledge.phonetics import double_metaphone
from radreport.onboarding.batches import ArtifactUpload, open_batch, record_counts, register_artifact, transition
from radreport.onboarding.lexicon import KNOWN_POLYSEMOUS, get_or_create_tenant_lexicon

log = get_logger(__name__)

_SHORT = r"(?P<short>[A-Z][A-Za-z0-9&.+]{0,9}[A-Za-z0-9])"
_FORMAL = r"(?P<formal>[A-Za-z][A-Za-z0-9 ,'()/+-]{2,80}?)"

#: (name, pattern, base confidence). Explicit separators are trusted more than layout.
PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (("equals", re.compile(rf"^\s*{_SHORT}\s*=\s*{_FORMAL}\s*\.?\s*$"), 0.9), ("arrow", re.compile(rf"^\s*{_SHORT}\s*(?:→|->|=>|⇒)\s*{_FORMAL}\s*\.?\s*$"), 0.9), ("pipe", re.compile(rf"^\s*\|?\s*{_SHORT}\s*\|\s*{_FORMAL}\s*\|?\s*$"), 0.85), ("colon", re.compile(rf"^\s*{_SHORT}\s*:\s*{_FORMAL}\s*\.?\s*$"), 0.75), ("dash", re.compile(rf"^\s*{_SHORT}\s+[-–—]\s+{_FORMAL}\s*\.?\s*$"), 0.75), ("parenthesised", re.compile(rf"^\s*{_FORMAL}\s*\(\s*{_SHORT}\s*\)\s*\.?\s*$"), 0.8), ("columns", re.compile(rf"^\s*{_SHORT}(?:\t+|\s{{2,}}){_FORMAL}\s*$"), 0.65))
_SLASH_GROUP = re.compile(r"^\s*([A-Z]{2,6}(?:\s*/\s*[A-Z]{2,6}){1,9})\s*$")
_STOP_FORMALS = frozenset({"normal", "none", "nil", "yes", "no", "findings", "impression"})

#: Accepted below this only when the separator alone is strong; layout guesses need the letters to line up.
MIN_CONFIDENCE = 0.5


@dataclass(frozen=True, slots=True)
class ShorthandMapping:
    short: str
    formal: str
    pattern: str
    line_no: int
    confidence: float
    raw: str


@dataclass(slots=True)
class ShorthandImport:
    batch: ImportBatch
    mappings: list[ShorthandMapping] = field(default_factory=list)
    terms_created: int = 0
    terms_updated: int = 0
    conflicts: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)


class UnreadableReference(ValueError):
    """A file whose text could not be read."""


def extract_text(data: bytes, filename: str) -> list[str]:
    """Lines of text from a .pdf, .docx, .txt, .md or .csv file."""
    lowered = filename.lower()
    if lowered.endswith(".pdf"):
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:  # noqa: BLE001 - a broken PDF is a refusal, not a crash
            raise UnreadableReference(f"could not read {filename!r} as a PDF: {type(exc).__name__}") from exc
        return text.splitlines()
    if lowered.endswith(".docx"):
        from radreport.onboarding.template_parse import UnsupportedDocument, _docx_paragraphs

        try:
            return [line.removeprefix("\x00HEADING\x00") for line in _docx_paragraphs(data)]
        except UnsupportedDocument as exc:
            raise UnreadableReference(str(exc)) from exc
    if lowered.endswith((".txt", ".md", ".csv", ".tsv")):
        lines = data.decode("utf-8", errors="replace").splitlines()
        if lowered.endswith(".csv"):
            # "LLL,Left lower lobe" -> the pipe pattern; quoting is rare in these sheets.
            lines = [line.replace('"', "").replace(",", " | ", 1) for line in lines]
        return lines
    raise UnreadableReference(f"unsupported shorthand reference format: {filename!r}; use PDF, Word or text")


def acronym_fit(short: str, formal: str) -> float:
    """How well the short form's letters spell the formal phrase, in order: 1.0 for LLL / left lower lobe."""
    letters = [c for c in short.upper() if c.isalpha()]
    if not letters:
        return 0.0
    raw_words = [w for w in re.split(r"[\s/-]+", formal) if w and w.upper() not in {"OF", "THE", "AND", "IN", "ON", "WITH"}]
    # An abbreviation inside the phrase ("high resolution CT") gives all its letters, not just its first.
    initials = "".join(w if w.isupper() and len(w) <= 4 else w[0].upper() for w in raw_words)
    # In-order match of the short form's letters against the initials, then against the whole phrase.
    hit = 0
    pos = 0
    for letter in letters:
        found = initials.find(letter, pos)
        if found >= 0:
            hit += 1
            pos = found + 1
    by_initials = hit / len(letters)
    joined = formal.upper()
    pos = 0
    hit = 0
    for letter in letters:
        found = joined.find(letter, pos)
        if found >= 0:
            hit += 1
            pos = found + 1
    # Spelled from inside one word ("PNA" in "pneumonia") counts only when the first letters agree.
    by_letters = hit / len(letters) * (0.8 if initials[:1] == letters[0] else 0.4)
    return round(max(by_initials, by_letters), 3)


def _clean(formal: str) -> str:
    return re.sub(r"\s+", " ", formal).strip(" .;,:-").strip()


def find_mappings(lines: list[str]) -> list[ShorthandMapping]:
    """Every `short -> formal` pair in a reference's lines, best-scored first per short form."""
    found: dict[str, ShorthandMapping] = {}
    groups: list[tuple[int, str]] = []
    for line_no, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or len(line) > 160:
            continue
        group = _SLASH_GROUP.match(line)
        if group:
            groups.append((line_no, group.group(1)))
            continue
        for name, pattern, base in PATTERNS:
            match = pattern.match(line)
            if not match:
                continue
            short, formal = match.group("short").strip(), _clean(match.group("formal"))
            if sum(c.isupper() for c in short) < 2:
                continue  # "Dr: Rao" is a name, not shorthand; shorthand is capitals
            if formal.lower() in _STOP_FORMALS or len(formal) <= len(short) or formal.upper() == formal and len(formal.split()) == 1:
                continue
            fit = acronym_fit(short, formal)
            # Layout-only patterns need the letters to line up; explicit separators can carry a looser fit ("PNA = pneumonia").
            confidence = round(base * (0.5 + 0.5 * fit), 3) if base < 0.8 else round(base * (0.7 + 0.3 * fit), 3)
            if confidence < MIN_CONFIDENCE:
                continue
            mapping = ShorthandMapping(short=short, formal=formal, pattern=name, line_no=line_no, confidence=confidence, raw=line)
            if short not in found or found[short].confidence < confidence:
                found[short] = mapping
            break
    # "RUL/LUL/RLL/LLL": each member is expanded from a definition elsewhere on the sheet, or from the lobe convention when the sheet relies on it.
    for line_no, group_text in groups:
        for member in (m.strip() for m in group_text.split("/")):
            if member in found:
                continue
            formal = _LOBES.get(member)
            if formal:
                found[member] = ShorthandMapping(short=member, formal=formal, pattern="slash_group", line_no=line_no, confidence=0.7, raw=group_text)
    return sorted(found.values(), key=lambda m: (m.line_no, m.short))


#: The lobe and quadrant abbreviations every radiology sheet assumes; used only to expand a bare slash group.
_LOBES = {"RUL": "right upper lobe", "RML": "right middle lobe", "RLL": "right lower lobe", "LUL": "left upper lobe", "LLL": "left lower lobe", "RUQ": "right upper quadrant", "LUQ": "left upper quadrant", "RLQ": "right lower quadrant", "LLQ": "left lower quadrant"}


def import_shorthand_reference(session: Session, *, tenant_id: uuid.UUID, uploads: list[ArtifactUpload], submitted_by: uuid.UUID | None = None, trigger: str = ImportTrigger.INITIAL_ONBOARDING) -> ShorthandImport:
    """Parse reference files and merge their pairs into the lab's lexicon, one audit entry per pair."""
    batch = open_batch(session, tenant_id=tenant_id, batch_type=ImportBatchType.SHORTHAND, stage="S3", trigger=trigger, submitted_by=submitted_by)
    transition(session, batch, ImportStatus.PARSING, actor_id=submitted_by)
    result = ShorthandImport(batch=batch)
    lexicon = get_or_create_tenant_lexicon(session, tenant_id)
    existing = {t.canonical_form.lower(): t for t in session.execute(select(LexiconTerm).where(LexiconTerm.lexicon_set_id == lexicon.id)).scalars()}
    by_short = {t.short_form.upper(): t for t in existing.values() if t.short_form}

    for upload in uploads:
        artifact, _created = register_artifact(session, batch, upload)
        try:
            mappings = find_mappings(extract_text(upload.data, upload.filename))
        except UnreadableReference as exc:
            result.failures.append((upload.filename, str(exc)))
            continue
        for mapping in mappings:
            result.mappings.append(mapping)
            term = existing.get(mapping.formal.lower())
            action = "shorthand_mapped"
            if term is None:
                holder = by_short.get(mapping.short.upper())
                if holder is not None and holder.canonical_form.lower() != mapping.formal.lower():
                    # One short form, two meanings in this lab: keep both visible rather than silently pick one.
                    result.conflicts.append(f"{mapping.short}: {holder.canonical_form!r} already, {mapping.formal!r} in {upload.filename} line {mapping.line_no}")
                    action = "shorthand_conflict"
                primary, secondary = double_metaphone(mapping.formal)
                term = LexiconTerm(tenant_id=tenant_id, lexicon_set_id=lexicon.id, canonical_form=mapping.formal, short_form=None if action == "shorthand_conflict" else mapping.short, term_type=TermType.ABBREVIATION, phonetic_key_primary=primary, phonetic_key_secondary=secondary, is_ambiguous=mapping.short.upper() in KNOWN_POLYSEMOUS or action == "shorthand_conflict")
                session.add(term)
                session.flush()
                existing[mapping.formal.lower()] = term
                if action != "shorthand_conflict":
                    by_short[mapping.short.upper()] = term
                result.terms_created += 1
            elif not term.short_form:
                term.short_form = mapping.short
                by_short[mapping.short.upper()] = term
                result.terms_updated += 1
            elif term.short_form.upper() != mapping.short.upper():
                result.conflicts.append(f"{mapping.formal}: short form {term.short_form!r} already, {mapping.short!r} in {upload.filename} line {mapping.line_no}")
                action = "shorthand_conflict"
            session.add(AuditLog(tenant_id=tenant_id, actor_id=submitted_by, actor_type=ActorType.USER if submitted_by else ActorType.SYSTEM, action=action, entity_type="lexicon_term", entity_id=term.id, after={"short": mapping.short, "formal": mapping.formal, "pattern": mapping.pattern, "confidence": mapping.confidence, "import_artifact_id": str(artifact.id), "filename": upload.filename, "line": mapping.line_no}))
    session.flush()
    record_counts(session, batch, accepted=len(result.mappings), rejected=len(result.failures))
    transition(session, batch, ImportStatus.AWAITING_REVIEW, actor_id=submitted_by)
    log.info("shorthand_reference_imported", tenant_id=str(tenant_id), batch_id=str(batch.id), mappings=len(result.mappings), created=result.terms_created, updated=result.terms_updated, conflicts=len(result.conflicts))
    return result


def shorthand_terms(session: Session, tenant_id: uuid.UUID) -> int:
    """How many of the lab's terms carry a short form."""
    return int(session.execute(select(func.count()).select_from(LexiconTerm).where(LexiconTerm.tenant_id == tenant_id, LexiconTerm.short_form.isnot(None))).scalar_one())
