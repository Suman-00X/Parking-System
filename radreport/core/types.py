"""The value sets shared across the whole system; each one is also a CHECK constraint in the database.

Defines: string enums for labs and people (TenantStatus, UserRole, PlatformRole), consent
(TrainingConsentEvent), patients and studies (Sex, StudyPriority, MetadataSource), capture
(CaptureDeviceClass, AudioFormat), speech recognition (AsrStatus, TranscriptStage,
ReconciliationMethod, UtteranceLabel) and the rest of the pipeline's vocabulary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class _Vals(StrEnum):
    """StrEnum with a `values()` helper for building CHECK constraints."""

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(m.value for m in cls)


# ============================================================== tenancy ======
class TenantStatus(_Vals):
    """The design doc defines `tenant.status` without values."""

    PROVISIONING = "provisioning"
    ONBOARDING = "onboarding"
    PILOT = "pilot"
    LIVE = "live"
    SUSPENDED = "suspended"
    OFFBOARDED = "offboarded"


class UserRole(_Vals):
    """`app_user.roles`, with the rename."""

    RADIOLOGIST = "radiologist"
    TRANSCRIPTIONIST = "transcriptionist"
    LAB_ADMIN = "lab_admin"
    AUDITOR = "auditor"


class PlatformRole(_Vals):
    """Product admins are not `app_user` rows — they belong to no tenant."""

    PRODUCT_ADMIN = "product_admin"
    SUPPORT = "support"


class TrainingConsentEvent(_Vals):
    """Append-only; you must be able to prove what was permitted when."""

    GRANTED = "granted"
    RENEWED = "renewed"
    WITHDRAWN = "withdrawn"


# ============================================================= clinical ======
class Sex(_Vals):
    M = "M"
    F = "F"
    O = "O"  # noqa: E741 - matches the design doc's CHECK values verbatim
    U = "U"


class StudyPriority(_Vals):
    ROUTINE = "routine"
    URGENT = "urgent"
    STAT = "stat"


class MetadataSource(_Vals):
    UPLOAD = "upload"
    DICOM = "dicom"
    INFERRED = "inferred"


# ============================================================ ingestion ======
class CaptureDeviceClass(_Vals):
    """Partitions the gold set; stratifies every metric."""

    LEGACY = "legacy"
    DICTATION_MIC_PTT = "dictation_mic_ptt"
    HEADSET = "headset"


class AudioFormat(_Vals):
    FLAC = "flac"
    WAV = "wav"


# ================================================================== ASR ======
class AsrStatus(_Vals):
    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"


class TranscriptStage(_Vals):
    RAW = "raw"
    RECONCILED = "reconciled"
    NORMALISED = "normalised"
    REPAIRED = "repaired"


class ReconciliationMethod(_Vals):
    SINGLE = "single"
    ROVER = "rover"
    LLM_ARBITRATED = "llm_arbitrated"


class UtteranceLabel(_Vals):
    """Nothing is deleted; everything is labelled (invariant I2)."""

    REPORT_CONTENT = "report_content"
    SELF_CORRECTION = "self_correction"
    COMMAND = "command"
    ASIDE = "aside"
    DISFLUENCY = "disfluency"
    OTHER_SPEAKER = "other_speaker"
    UNCERTAIN = "uncertain"


class LabelSource(_Vals):
    LLM = "llm"
    DISTILLED_MODEL = "distilled_model"
    RULE = "rule"
    HUMAN = "human"


# ============================================================ knowledge ======
class LexiconScope(_Vals):
    GLOBAL = "global"
    MODALITY = "modality"
    TEMPLATE = "template"
    SPEAKER = "speaker"


class TermType(_Vals):
    ANATOMY = "anatomy"
    PATHOLOGY = "pathology"
    ABBREVIATION = "abbreviation"
    CODE_WORD = "code_word"
    MEASUREMENT_UNIT = "measurement_unit"
    DEVICE = "device"
    DRUG = "drug"
    PERSON = "person"
    """Person-name spans drive the pre-pooling audio PHI scrub."""


class ExpansionPolicy(_Vals):
    ALWAYS_EXPAND = "always_expand"
    NEVER_EXPAND = "never_expand"
    PER_TEMPLATE = "per_template"


class VariantSource(_Vals):
    MINED = "mined"
    MANUAL = "manual"
    GENERATED = "generated"


class FieldDataType(_Vals):
    TEXT = "text"
    ENUM = "enum"
    MEASUREMENT = "measurement"
    BOOLEAN_TRI = "boolean_tri"
    LIST = "list"


class AbsencePolicy(_Vals):
    """/ — a clinical safety decision, not an engineering one."""

    DEFAULT_NORMAL = "default_normal"
    LEAVE_BLANK_FLAG = "leave_blank_flag"
    BLOCK = "block"


# ============================================================ reporting ======
class Stage1FilterSource(_Vals):
    DICOM = "dicom"
    LEXICAL_FINGERPRINT = "lexical_fingerprint"
    CODE_WORD = "code_word"
    DEMOGRAPHIC = "demographic"


class DraftStatus(_Vals):
    GENERATED = "generated"
    IN_REVIEW = "in_review"
    REVISED = "revised"
    SIGNED = "signed"
    DISCARDED = "discarded"


class AssertionStatus(_Vals):
    """Never a bare boolean."""

    PRESENT = "present"
    ABSENT = "absent"
    UNCERTAIN = "uncertain"
    NOT_ASSESSED = "not_assessed"


class Laterality(_Vals):
    LEFT = "left"
    RIGHT = "right"
    BILATERAL = "bilateral"
    MIDLINE = "midline"
    NA = "na"


class FillSource(_Vals):
    """Audit-critical."""

    DICTATED = "dictated"
    TEMPLATE_DEFAULT = "template_default"
    BLANKET_NORMAL = "blanket_normal"
    INFERRED = "inferred"
    HUMAN = "human"


class CheckType(_Vals):
    RULE = "rule"
    SCHEMA = "schema"
    LLM_CRITIC = "llm_critic"
    ROUNDTRIP = "roundtrip"
    CROSS_MODAL = "cross_modal"


class Severity(_Vals):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    BLOCK = "block"


class HumanVerdict(_Vals):
    TRUE_POSITIVE = "true_positive"
    FALSE_POSITIVE = "false_positive"
    UNREVIEWED = "unreviewed"


class PatternType(_Vals):
    LEXICAL = "lexical"
    REGEX = "regex"
    LLM_SEMANTIC = "llm_semantic"


class AlertSeverity(_Vals):
    RED = "red"
    ORANGE = "orange"


# ============================================================= autonomy ======
class AutonomyStatus(_Vals):
    NOT_EVALUATED = "not_evaluated"
    ACCRUING = "accruing"
    GRANTED = "granted"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class SeverityGrade(_Vals):
    """G3/G4 are clinically significant errors (CSE)."""

    G0 = "G0"
    G1 = "G1"
    G2 = "G2"
    G3 = "G3"
    G4 = "G4"


CSE_GRADES: Final[frozenset[str]] = frozenset({SeverityGrade.G3, SeverityGrade.G4})


# =============================================================== review ======
class ReviewerRole(_Vals):
    TRANSCRIPTIONIST = "transcriptionist"
    RADIOLOGIST = "radiologist"


class EditType(_Vals):
    VALUE_CHANGE = "value_change"
    FIELD_ADDED = "field_added"
    FIELD_REMOVED = "field_removed"
    TEMPLATE_CHANGED = "template_changed"
    TEXT_REWRITE = "text_rewrite"


class ErrorCategory(_Vals):
    ASR_TERM = "asr_term"
    ASR_NUMBER = "asr_number"
    LATERALITY = "laterality"
    NEGATION = "negation"
    EXTRACTION_MISS = "extraction_miss"
    HALLUCINATION = "hallucination"
    STYLE = "style"
    TEMPLATE_WRONG = "template_wrong"


class PathType(_Vals):
    TRANSCRIPTIONIST_REVIEWED = "transcriptionist_reviewed"
    RADIOLOGIST_ONLY = "radiologist_only"
    AUTONOMOUS = "autonomous"
    """Released with no human review, under a `granted` autonomy class."""


class ExportStatus(_Vals):
    PENDING = "pending"
    SENT = "sent"
    ACK = "ack"
    FAILED = "failed"


# ======================================================== orchestration ======
class PipelineTrigger(_Vals):
    UPLOAD = "upload"
    MANUAL_RETRY = "manual_retry"
    BACKFILL = "backfill"
    SHADOW = "shadow"


class RunStatus(_Vals):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActorType(_Vals):
    USER = "user"
    SYSTEM = "system"
    INTEGRATION = "integration"


# =========================================================== onboarding ======
class ImportBatchType(_Vals):
    TEMPLATE = "template"
    REPORT_CORPUS = "report_corpus"
    PAIRED_AUDIO = "paired_audio"
    SHORTHAND = "shorthand"
    ROSTER = "roster"
    BOILERPLATE = "boilerplate"
    CRITICAL_RULES = "critical_rules"


class ImportTrigger(_Vals):
    INITIAL_ONBOARDING = "initial_onboarding"
    TEMPLATE_CHANGE = "template_change"
    NEW_RADIOLOGIST = "new_radiologist"
    PERIODIC_REMINE = "periodic_remine"
    SITE_EXPANSION = "site_expansion"


class ImportStatus(_Vals):
    UPLOADING = "uploading"
    PARSING = "parsing"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"


class ParseStatus(_Vals):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class CandidateReviewStatus(_Vals):
    PENDING = "pending"
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"
    MERGED = "merged"


class MergeDecision(_Vals):
    MERGE = "merge"
    KEEP_SEPARATE = "keep_separate"
    PENDING = "pending"


class MatchMethod(_Vals):
    EXPLICIT = "explicit"
    STRUCTURAL = "structural"
    LLM = "llm"
    HUMAN = "human"


class CollisionClass(_Vals):
    E_SET_LETTER = "e_set_letter"
    HOMOPHONE = "homophone"
    NEAR_HOMOPHONE = "near_homophone"
    NATURAL_WORD_OVERLAP = "natural_word_overlap"


class CollisionSeverity(_Vals):
    BLOCK = "block"
    WARN = "warn"


class CollisionResolution(_Vals):
    RENAMED = "renamed"
    ACCEPTED_WITH_MARGIN_GUARD = "accepted_with_margin_guard"
    PENDING = "pending"


class ReviewStatus(_Vals):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class CheckStatus(_Vals):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


# =========================================================== adaptation ======
class VerbatimSource(_Vals):
    HUMAN_ANNOTATION = "human_annotation"
    VERIFIED_CORRECTION = "verified_correction"
    IMPORTED = "imported"


class AdaptationTarget(_Vals):
    ASR_GLOBAL = "asr_global"
    ASR_SPEAKER = "asr_speaker"
    UTTERANCE_CLASSIFIER = "utterance_classifier"
    ROUTER = "router"


class AdaptationMethod(_Vals):
    AZURE_CUSTOM_SPEECH = "azure_custom_speech"
    WHISPER_LORA = "whisper_lora"
    FULL_FT = "full_ft"
    DISTILLATION = "distillation"


class AdaptationStatus(_Vals):
    TRAINING = "training"
    EVALUATED = "evaluated"
    SHADOW = "shadow"
    PROMOTED = "promoted"
    REJECTED = "rejected"


# ========================================================= model config ======
class ProviderKind(_Vals):
    CLOUD_API = "cloud_api"
    LOCAL_OPENAI_COMPATIBLE = "local_openai_compatible"


class AuthMethod(_Vals):
    API_KEY = "api_key"
    NONE = "none"


class TaskKey(_Vals):
    """The LLM tasks plus two `asr_*` keys, so per-task model and engine swaps share one mechanism."""

    ASR_PRIMARY = "asr_primary"
    """The engine that transcribes. One per lab."""

    ASR_SECONDARY = "asr_secondary"
    """Additional engines for Beta's ROVER fan-out. Several may be assigned; the active-uniqueness rule is relaxed for this key alone."""

    EXTRACTION = "extraction"
    SELF_CORRECTION = "self_correction"
    VERIFICATION = "verification"
    ROUTING_PICK = "routing_pick"
    UTTERANCE_CLASSIFICATION = "utterance_classification"
    ROUTING_SHORTLIST = "routing_shortlist"
    ROUNDTRIP_CHECK = "roundtrip_check"
    COMPOSE = "compose"
    """Not in the list and not in the exclusion list."""


class TaskBucket(_Vals):
    CONSEQUENTIAL = "consequential"
    BOUNDED = "bounded"


#: the scope discipline: these stay on a frontier model regardless of what
#: local hardware can do. Enforced in `adapters/llm/registry.py`.
CONSEQUENTIAL_TASKS: Final[frozenset[str]] = frozenset(
    {
        TaskKey.EXTRACTION,
        TaskKey.SELF_CORRECTION,
        TaskKey.VERIFICATION,
        # Transcription is upstream of every other task: an error here is an error in everything that reads the transcript, so it belongs in the bucket that requires the most evidence before a change.
        TaskKey.ASR_PRIMARY,
    }
)


#: Tasks whose engine is an ASR model rather than an LLM. They share the
#: assignment tables; they do not share a client.
ASR_TASKS: Final[frozenset[str]] = frozenset({TaskKey.ASR_PRIMARY, TaskKey.ASR_SECONDARY})


class AssignmentStatus(_Vals):
    PROPOSED = "proposed"
    TESTING = "testing"
    ACTIVE = "active"
    RETIRED = "retired"


class AssignmentEvent(_Vals):
    PROPOSED = "proposed"
    TESTED = "tested"
    ACTIVATED = "activated"
    RETIRED = "retired"


# ================================================================ eval =======
class AudioQualityBucket(_Vals):
    CLEAN = "clean"
    MODERATE = "moderate"
    NOISY = "noisy"


#: Display relabelling for the review UI. The schema keeps the
#: design doc's values; only the label changes.
DISPLAY_LABELS: Final[dict[str, str]] = {ReviewerRole.TRANSCRIPTIONIST: "Radiologist assistant", ReviewerRole.RADIOLOGIST: "Radiologist", PathType.TRANSCRIPTIONIST_REVIEWED: "Assistant-reviewed", PathType.RADIOLOGIST_ONLY: "Radiologist-only", PathType.AUTONOMOUS: "Released without review", UserRole.TRANSCRIPTIONIST: "Radiologist assistant", UserRole.LAB_ADMIN: "Lab administrator"}
