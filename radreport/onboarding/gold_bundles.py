"""Takes a lab's gold bundles -- for each template, a few dictations with their verbatim transcript and the final report -- and files each part where the rest of onboarding reads it.

Order: read the ZIP's manifest and audio files (parse_bundle_zip) -> for each bundle, register its
study, store the dictation, record the verbatim, record the final report and map it to the
template the lab named (import_gold_bundles) -> group the bundles by template for the acceptance
set (candidates_by_template) and count them (bundles_per_template, held_per_template). Re-uploading a bundle reuses
what is already stored, so a fixed manifest can be sent again whole.
"""

from __future__ import annotations

import csv
import io
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radreport.adapters.storage.object_store import ObjectStore
from radreport.core.errors import DuplicateRecording, IngestRejected
from radreport.core.logging import get_logger
from radreport.core.types import CaptureDeviceClass, ImportBatchType, ImportStatus, ImportTrigger, MatchMethod, VerbatimSource
from radreport.db.models.adaptation import VerbatimTranscript
from radreport.db.models.evaluation import EvalItem
from radreport.db.models.identity import AppUser, RadiologistProfile, Study
from radreport.db.models.ingestion import Recording
from radreport.db.models.knowledge import Template, TemplateVersion
from radreport.db.models.onboarding import CorpusReport, CorpusReportTemplateMap, ImportBatch
from radreport.eval.goldset import Candidate, eligible_candidates
from radreport.ingest.service import IngestRequest, ingest_recording
from radreport.ingest.studies import StudyIn, register_study
from radreport.onboarding.batches import ArtifactUpload, open_batch, record_counts, register_artifact, transition
from radreport.onboarding.paired_audio import submit_verbatim

log = get_logger(__name__)

#: Columns the manifest must carry, and the ones it may.
REQUIRED_COLUMNS = ("template_code", "audio_file", "verbatim_text", "report_text", "radiologist_employee_code", "external_report_id")
OPTIONAL_COLUMNS = ("capture_device_class", "includes_disfluencies", "is_deidentified")
MANIFEST_NAME = "manifest.csv"

#: Limits on what a ZIP may unpack to, so one upload cannot exhaust the instance's memory.
MAX_MEMBERS = 2_000
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
_FALSE = frozenset({"false", "no", "0", "n"})
_TRUE = frozenset({"true", "yes", "1", "y"})


class BundleFileRefused(ValueError):
    """The ZIP as a whole cannot be read."""


@dataclass(frozen=True, slots=True)
class GoldBundle:
    """One dictation, what was said in it word for word, and the report it produced."""

    template_code: str
    audio_file: str
    audio: bytes
    verbatim_text: str
    report_text: str
    radiologist_employee_code: str
    external_report_id: str
    capture_device_class: str = CaptureDeviceClass.DICTATION_MIC_PTT
    includes_disfluencies: bool = True
    is_deidentified: bool = False


@dataclass(slots=True)
class BundleImport:
    batch: ImportBatch
    accepted: int = 0
    reused: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)
    per_template: Counter[str] = field(default_factory=Counter)


def parse_bundle_zip(data: bytes) -> tuple[list[GoldBundle], list[str]]:
    """Read `manifest.csv` and the audio it names out of the ZIP. Returns `(bundles, problems)`."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BundleFileRefused("not a ZIP file") from exc
    members = [m for m in archive.infolist() if not m.is_dir()]
    if len(members) > MAX_MEMBERS:
        raise BundleFileRefused(f"the ZIP holds {len(members)} files; at most {MAX_MEMBERS}")
    if sum(m.file_size for m in members) > MAX_UNPACKED_BYTES:
        raise BundleFileRefused(f"the ZIP unpacks to more than {MAX_UNPACKED_BYTES // (1024 * 1024)} MB")
    # Matched by base name, so a manifest inside one top-level folder works too.
    by_name = {m.filename.rsplit("/", 1)[-1]: m for m in members if ".." not in m.filename.split("/")}
    manifest = by_name.get(MANIFEST_NAME)
    if manifest is None:
        raise BundleFileRefused(f"no {MANIFEST_NAME} in the ZIP")
    reader = csv.DictReader(io.StringIO(archive.read(manifest).decode("utf-8-sig", errors="replace")))
    columns = {(c or "").strip().lower() for c in reader.fieldnames or []}
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise BundleFileRefused(f"{MANIFEST_NAME} is missing column(s): {', '.join(missing)}")

    bundles: list[GoldBundle] = []
    problems: list[str] = []
    for line_no, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        unknown = sorted(set(row) - set(REQUIRED_COLUMNS) - set(OPTIONAL_COLUMNS))
        if unknown:
            problems.append(f"row {line_no}: unknown column(s) {', '.join(unknown)}")
            continue
        empty = [c for c in REQUIRED_COLUMNS if not row.get(c)]
        if empty:
            problems.append(f"row {line_no}: {', '.join(empty)} empty")
            continue
        audio = by_name.get(row["audio_file"].rsplit("/", 1)[-1])
        if audio is None:
            problems.append(f"row {line_no}: audio file {row['audio_file']!r} is not in the ZIP")
            continue
        device = row.get("capture_device_class") or CaptureDeviceClass.DICTATION_MIC_PTT
        if device not in CaptureDeviceClass.values():
            problems.append(f"row {line_no}: capture_device_class {device!r} is not one of {', '.join(CaptureDeviceClass.values())}")
            continue
        bundles.append(GoldBundle(template_code=row["template_code"], audio_file=row["audio_file"], audio=archive.read(audio), verbatim_text=row["verbatim_text"], report_text=row["report_text"], radiologist_employee_code=row["radiologist_employee_code"], external_report_id=row["external_report_id"], capture_device_class=device, includes_disfluencies=(row.get("includes_disfluencies") or "true").lower() not in _FALSE, is_deidentified=(row.get("is_deidentified") or "").lower() in _TRUE))
    return bundles, problems


def active_templates(session: Session, tenant_id: uuid.UUID) -> dict[str, Template]:
    """The lab's active templates that have a live version, by code."""
    rows = session.execute(select(Template).join(TemplateVersion, TemplateVersion.template_id == Template.id).where(Template.tenant_id == tenant_id, Template.is_active.is_(True), TemplateVersion.is_current.is_(True))).scalars()
    return {t.code: t for t in rows}


def _radiologists(session: Session, tenant_id: uuid.UUID) -> dict[str, uuid.UUID]:
    rows = session.execute(select(AppUser.employee_code, RadiologistProfile.id).join(RadiologistProfile, RadiologistProfile.user_id == AppUser.id).where(AppUser.tenant_id == tenant_id)).all()
    return {code: profile_id for code, profile_id in rows}


def import_gold_bundles(session: Session, store: ObjectStore, *, tenant_id: uuid.UUID, bundles: list[GoldBundle], upload: ArtifactUpload | None = None, trigger: str = ImportTrigger.INITIAL_ONBOARDING) -> BundleImport:
    """File every bundle: study, dictation, verbatim, final report and its template; a bad bundle is rejected alone."""
    batch = open_batch(session, tenant_id=tenant_id, batch_type=ImportBatchType.PAIRED_AUDIO, stage="S4", trigger=trigger)
    transition(session, batch, ImportStatus.PARSING)
    if upload is not None:
        register_artifact(session, batch, upload)
    result = BundleImport(batch=batch)
    templates = active_templates(session, tenant_id)
    radiologists = _radiologists(session, tenant_id)

    for bundle in bundles:
        template = templates.get(bundle.template_code)
        if template is None:
            result.rejected.append((bundle.external_report_id, f"no active template {bundle.template_code!r}; approve the template before sending its bundles"))
            continue
        radiologist_id = radiologists.get(bundle.radiologist_employee_code)
        if radiologist_id is None:
            result.rejected.append((bundle.external_report_id, f"no radiologist with employee code {bundle.radiologist_employee_code!r} in the roster"))
            continue
        # A savepoint per bundle, so one bad bundle leaves the others in place.
        try:
            with session.begin_nested():
                reused = _file_bundle(session, store, tenant_id=tenant_id, batch_id=batch.id, bundle=bundle, template=template, radiologist_id=radiologist_id)
        except IngestRejected as exc:
            result.rejected.append((bundle.external_report_id, f"{bundle.audio_file}: {exc.reason}"))
            continue
        except ValueError as exc:
            result.rejected.append((bundle.external_report_id, str(exc)))
            continue
        result.accepted += 1
        result.reused += int(reused)
        result.per_template[template.code] += 1

    record_counts(session, batch, accepted=result.accepted, rejected=len(result.rejected))
    batch.item_count += len(bundles)
    if result.accepted:
        for status in (ImportStatus.AWAITING_REVIEW, ImportStatus.APPROVED, ImportStatus.APPLIED):
            transition(session, batch, status)
    else:
        transition(session, batch, ImportStatus.REJECTED)
    log.info("gold_bundles_imported", tenant_id=str(tenant_id), batch_id=str(batch.id), accepted=result.accepted, reused=result.reused, rejected=len(result.rejected), per_template=dict(result.per_template))
    return result


def _file_bundle(session: Session, store: ObjectStore, *, tenant_id: uuid.UUID, batch_id: uuid.UUID, bundle: GoldBundle, template: Template, radiologist_id: uuid.UUID) -> bool:
    """Write one bundle's rows; True when its audio was already stored."""
    registered = register_study(session, tenant_id, StudyIn(mrn=f"GOLD-{bundle.external_report_id}", accession_number=bundle.external_report_id, modality=template.modality, body_part_examined=template.body_region, study_description=template.display_name), actor_id=None)
    reused = False
    try:
        recording_id = ingest_recording(session, store, IngestRequest(tenant_id=tenant_id, study_id=registered.study_id, radiologist_id=radiologist_id, filename=bundle.audio_file, data=bundle.audio, capture_device_class=bundle.capture_device_class, is_push_to_talk=bundle.capture_device_class == CaptureDeviceClass.DICTATION_MIC_PTT)).recording.id
    except DuplicateRecording as exc:
        recording_id = uuid.UUID(exc.recording_id)
        reused = True
    submit_verbatim(session, tenant_id=tenant_id, recording_id=recording_id, text=bundle.verbatim_text, annotator_id=None, includes_disfluencies=bundle.includes_disfluencies, source=VerbatimSource.IMPORTED)

    report = session.execute(select(CorpusReport).where(CorpusReport.tenant_id == tenant_id, CorpusReport.external_report_id == bundle.external_report_id)).scalar_one_or_none()
    if report is None:
        report = CorpusReport(tenant_id=tenant_id, import_batch_id=batch_id, external_report_id=bundle.external_report_id, report_text=bundle.report_text, radiologist_id=radiologist_id, is_deidentified=bundle.is_deidentified)
        session.add(report)
        session.flush()
    else:
        report.report_text, report.radiologist_id, report.is_deidentified = bundle.report_text, radiologist_id, bundle.is_deidentified
    # The lab named the template, so the mapping is a fact, not a guess to verify.
    mapping = session.execute(select(CorpusReportTemplateMap).where(CorpusReportTemplateMap.tenant_id == tenant_id, CorpusReportTemplateMap.corpus_report_id == report.id)).scalar_one_or_none()
    if mapping is None:
        session.add(CorpusReportTemplateMap(tenant_id=tenant_id, corpus_report_id=report.id, template_id=template.id, match_method=MatchMethod.HUMAN, confidence=1.0, is_verified=True))
    else:
        mapping.template_id, mapping.match_method, mapping.confidence, mapping.is_verified = template.id, MatchMethod.HUMAN, 1.0, True
    session.flush()
    return reused


def _recording_templates(session: Session, tenant_id: uuid.UUID) -> dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID, str]]:
    """`recording_id -> (template_id, current template_version_id, template code)` for every dictation whose report the lab mapped."""
    rows = session.execute(select(Recording.id, Template.id, TemplateVersion.id, Template.code).join(Study, (Study.id == Recording.study_id) & (Study.tenant_id == Recording.tenant_id)).join(CorpusReport, (CorpusReport.external_report_id == Study.accession_number) & (CorpusReport.tenant_id == Study.tenant_id)).join(CorpusReportTemplateMap, CorpusReportTemplateMap.corpus_report_id == CorpusReport.id).join(Template, Template.id == CorpusReportTemplateMap.template_id).join(TemplateVersion, (TemplateVersion.template_id == Template.id) & TemplateVersion.is_current.is_(True)).where(Recording.tenant_id == tenant_id, CorpusReportTemplateMap.is_verified.is_(True), Template.is_active.is_(True))).all()
    return {rec: (tid, vid, code) for rec, tid, vid, code in rows}


def candidates_by_template(session: Session, tenant_id: uuid.UUID) -> dict[uuid.UUID, tuple[uuid.UUID, str, list[Candidate]]]:
    """Verbatim-transcribed, current-hardware dictations, grouped as `template_id -> (version_id, code, candidates)`."""
    owners = _recording_templates(session, tenant_id)
    groups: dict[uuid.UUID, tuple[uuid.UUID, str, list[Candidate]]] = {}
    for candidate in eligible_candidates(session, tenant_id=tenant_id):
        owner = owners.get(candidate.recording_id)
        if owner is None or candidate.capture_device_class == CaptureDeviceClass.LEGACY:
            continue
        template_id, version_id, code = owner
        groups.setdefault(template_id, (version_id, code, []))[2].append(candidate)
    return groups


def bundles_per_template(session: Session, tenant_id: uuid.UUID) -> dict[str, int]:
    """How many usable bundles each active template has, zero included, counted in one read."""
    live = select(TemplateVersion.id).where(TemplateVersion.template_id == Template.id, TemplateVersion.is_current.is_(True)).exists()
    rows = session.execute(
        select(Template.code, func.count(func.distinct(VerbatimTranscript.id)))
        .outerjoin(CorpusReportTemplateMap, (CorpusReportTemplateMap.template_id == Template.id) & CorpusReportTemplateMap.is_verified.is_(True))
        .outerjoin(CorpusReport, CorpusReport.id == CorpusReportTemplateMap.corpus_report_id)
        .outerjoin(Study, (Study.accession_number == CorpusReport.external_report_id) & (Study.tenant_id == CorpusReport.tenant_id))
        .outerjoin(Recording, (Recording.study_id == Study.id) & (Recording.tenant_id == Study.tenant_id) & (Recording.capture_device_class != CaptureDeviceClass.LEGACY))
        .outerjoin(VerbatimTranscript, (VerbatimTranscript.recording_id == Recording.id) & VerbatimTranscript.includes_disfluencies.is_(True))
        .where(Template.tenant_id == tenant_id, Template.is_active.is_(True), live)
        .group_by(Template.code)
    ).all()
    return {code: int(n) for code, n in rows}


def held_per_template(session: Session, tenant_id: uuid.UUID, eval_set_id: uuid.UUID) -> dict[str, int]:
    """Items an acceptance set holds for each active template, zero included."""
    counts = {code: 0 for code in active_templates(session, tenant_id)}
    rows = session.execute(select(Template.code, func.count(EvalItem.id)).join(TemplateVersion, TemplateVersion.template_id == Template.id).join(EvalItem, EvalItem.gold_template_version_id == TemplateVersion.id).where(EvalItem.eval_set_id == eval_set_id, Template.tenant_id == tenant_id).group_by(Template.code)).all()
    for code, n in rows:
        if code in counts:
            counts[code] = int(n)
    return counts
