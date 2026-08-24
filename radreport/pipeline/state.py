"""The single object every stage reads from and adds to as a report moves down the pipeline.

Defines: the audio and its recognition (AudioMeta, AsrRunRef, Utterance, WordTimingState,
TranscriptState), what was recognised in it (TermResolution, StudyCodeDetection,
FindingSketch), the template chosen (RoutingState), and the draft's values with the audio they
came from (FieldValue, ProvenanceRef, RoutingToHuman).
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from radreport.core.types import AssertionStatus, CaptureDeviceClass, FillSource, Laterality, Severity, UtteranceLabel


class AudioMeta(BaseModel):
    """What ingest measured. Also the quality-gate record."""

    duration_seconds: float
    sample_rate_hz: int
    channels: int
    codec: str
    audio_format: str
    measured_snr_db: float | None = None
    silence_ratio: float | None = None
    capture_device_class: str = CaptureDeviceClass.LEGACY
    is_push_to_talk: bool = False


class AsrRunRef(BaseModel):
    asr_run_id: uuid.UUID
    engine: str
    engine_version: str
    overall_confidence: float | None = None


class Utterance(BaseModel):
    """One labelled span. Nothing is deleted; everything is labelled (I2)."""

    id: uuid.UUID | None = None
    seq: int
    char_start: int
    char_end: int
    audio_start_ms: int
    audio_end_ms: int
    text: str
    label: str = UtteranceLabel.REPORT_CONTENT
    label_confidence: float = 1.0
    label_source: str = "rule"
    contains_clinical_tokens: bool = False
    superseded_by_seq: int | None = None
    """Set on the *retracted* half of "left — sorry, right kidney". Naive filtering keeps `left`, which is a G4 error class."""

    is_included_downstream: bool = True


class WordTimingState(BaseModel):
    """One word's position in the transcript and in the audio."""

    char_start: int
    char_end: int
    start_ms: int
    end_ms: int


class TermResolution(BaseModel):
    """One phonetic resolution, recorded **beside** the transcript."""

    char_start: int
    char_end: int
    surface: str
    canonical_form: str | None = None
    margin: float = 0.0
    escalated: bool = False
    """True ⇒ the top two candidates sat within `TAU_MARGIN` and the resolver refused to pick."""

    alternatives: list[str] = Field(default_factory=list)
    is_ambiguous_term: bool = False
    """PA, RA, CA — flagged at term mining, needing context rather than a phonetic decision."""


class StudyCodeDetection(BaseModel):
    """The routing anchor, with the two metrics kept apart."""

    carrier_phrase_heard: bool = False
    """`CODEWORD_COMPLIANCE` — did the radiologist say the carrier phrase?"""

    spoken_code_heard: str | None = None
    """`STUDYCODE_RECALL` — did we hear the code after it? An engine problem."""

    template_version_id: uuid.UUID | None = None
    confidence: float = 0.0
    margin: float = 0.0
    escalated: bool = False
    search_window_ms: int = 0


class TranscriptState(BaseModel):
    id: uuid.UUID | None = None
    version: int = 1
    stage: str = "raw"
    text: str
    source_asr_run_ids: list[uuid.UUID] = Field(default_factory=list)
    reconciliation_method: str | None = None
    disagreement_score: float | None = None


class FindingSketch(BaseModel):
    """Template-free first pass (stage 8)."""

    assertions: list[str] = Field(default_factory=list)
    measurements: list[dict[str, object]] = Field(default_factory=list)


class RoutingState(BaseModel):
    chosen_template_version_id: uuid.UUID | None = None
    attached_module_version_ids: list[uuid.UUID] = Field(default_factory=list)
    candidate_set: list[dict[str, object]] = Field(default_factory=list)
    stage1_filter_source: str | None = None
    stage1_candidate_count: int | None = None
    confidence: float = 0.0
    orphan_assertion_count: int = 0
    required_field_gap_count: int = 0


class ProvenanceRef(BaseModel):
    """Invariant I1. A field value with none of these is `is_grounded = false` and never renders."""

    transcript_id: uuid.UUID | None = None
    utterance_seq: int | None = None
    char_start: int
    char_end: int
    audio_start_ms: int
    audio_end_ms: int
    quote: str
    """Must appear **verbatim** in the cited char range. Checked deterministically, outside the LLM."""

    extraction_confidence: float | None = None


class FieldValue(BaseModel):
    field_key: str
    value_text: str | None = None
    value_numeric: float | None = None
    value_unit: str | None = None
    value_enum: str | None = None
    assertion_status: str = AssertionStatus.NOT_ASSESSED
    laterality: str | None = Laterality.NA
    fill_source: str = FillSource.DICTATED
    is_grounded: bool = False
    confidence: float = 0.0
    is_flagged: bool = False
    flag_reasons: list[str] = Field(default_factory=list)
    provenance: list[ProvenanceRef] = Field(default_factory=list)


class RoutingToHuman(BaseModel):
    """Stage 15's decision: who reviews this draft, or that nobody does."""

    model_config = ConfigDict(frozen=True)

    reviewer_role: str | None
    """None exactly when `path_type` is `autonomous` — there is no reviewer, and naming one would make the queue offer work that does not exist."""

    path_type: str
    priority: str
    reason: str
    selected_for_grading: bool
    confidence: float

    released_without_review: bool = False
    release_blocker: str | None = None
    """Why not, as a stable code from `autonomy.release`."""

    autonomy_class_code: str | None = None


class DictatingRadiologist(BaseModel):
    """Who dictated this recording, and whether they allow autonomous release."""

    profile_id: uuid.UUID
    user_id: uuid.UUID
    autonomy_enabled: bool = False
    """`radiologist_profile.autonomy_enabled`."""


class VerificationFindingState(BaseModel):
    check_id: str
    check_type: str
    severity: str = Severity.WARN
    message: str
    field_key: str | None = None
    evidence: dict[str, object] | None = None


class CriticalAlertState(BaseModel):
    """Fires on the **transcript**, before anything enters a queue."""

    rule_code: str
    evidence_text: str
    confidence: float
    utterance_seq: int | None = None


class PipelineState(BaseModel):
    """The object every stage reads and the orchestrator writes."""

    tenant_id: uuid.UUID
    recording_id: uuid.UUID
    pipeline_run_id: uuid.UUID
    is_shadow: bool = False

    study_id: uuid.UUID | None = None
    """The recording's study."""

    radiologist: DictatingRadiologist | None = None
    """Who dictated."""

    audio_object_key: str | None = None
    """Where the audio lives."""

    audio_meta: AudioMeta | None = None
    asr_runs: list[AsrRunRef] = Field(default_factory=list)
    transcript: TranscriptState | None = None
    utterances: list[Utterance] = Field(default_factory=list)
    resolutions: list[TermResolution] = Field(default_factory=list)
    word_timings: list[WordTimingState] = Field(default_factory=list)
    study_code: StudyCodeDetection | None = None
    sketch: FindingSketch | None = None
    routing: RoutingState | None = None
    field_values: dict[str, FieldValue] = Field(default_factory=dict)
    verification: list[VerificationFindingState] = Field(default_factory=list)
    critical_alerts: list[CriticalAlertState] = Field(default_factory=list)

    critic_iterations: int = 0
    """Hard cap enforced in `graph.py`, not here — a stage must not be able to grant itself another loop."""

    rendered_text: str | None = None
    overall_confidence: float | None = None
    total_cost_usd: float = 0.0

    report_draft_id: uuid.UUID | None = None
    """Set by stage 16 once the draft row is built, so stage 17 can reference it without a second query."""

    human_routing: RoutingToHuman | None = None
    """Stage 15's decision, carried rather than recomputed."""

    def included_utterances(self) -> list[Utterance]:
        """The only utterances downstream stages may cite."""
        return [u for u in self.utterances if u.is_included_downstream]
