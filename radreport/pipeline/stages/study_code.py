"""Stage 4: finds the spoken code that says which kind of study this is.

Order: look for the code in the opening of the dictation (detect_study_code), then report how
often radiologists are saying it (compliance_metrics).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from radreport.core.logging import get_logger
from radreport.core.types import Stage1FilterSource
from radreport.knowledge.phonetics import TAU_MARGIN, phonetic_distance
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.stages.providers import KnowledgeProvider, StudyCodeEntry
from radreport.pipeline.state import PipelineState, RoutingState, StudyCodeDetection

log = get_logger(__name__)

#: the bounded window. Generous enough for a slow start, short enough that
#: the body of the report is never searched.
DEFAULT_WINDOW_MS = 20_000

#: The carrier phrase, and the misrecognitions of it that still count as compliance.
CARRIER_PATTERNS: tuple[str, ...] = (r"study\s*type", r"study\s*code", r"stud[iy]\s*type")
_CARRIER = re.compile("|".join(f"(?:{p})" for p in CARRIER_PATTERNS), re.IGNORECASE)

#: How much text after the carrier phrase can hold the code.
_CODE_LOOKAHEAD_CHARS = 60


@dataclass(frozen=True, slots=True)
class CodeMatch:
    entry: StudyCodeEntry
    distance: float
    margin: float
    surface: str


class StudyCodeStage:
    """Deterministic; no model call."""

    name = "study_code"
    version = "1.0.0"

    def __init__(self, knowledge: KnowledgeProvider, *, window_ms: int = DEFAULT_WINDOW_MS, tau_margin: float = TAU_MARGIN, max_distance: float = 0.20) -> None:
        self._knowledge = knowledge
        self._window_ms = window_ms
        self._tau = tau_margin
        self._max_distance = max_distance

    def is_idempotent(self) -> bool:
        return True

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if state.transcript is None:
            raise ValueError("study-code detection runs on a transcript; none is present")

        knowledge = self._knowledge.for_tenant(state.tenant_id)
        window_text = self._window_text(state)

        detection = detect_study_code(window_text, knowledge.study_codes, tau_margin=self._tau, max_distance=self._max_distance)
        detection.search_window_ms = self._window_ms
        state.study_code = detection

        routing = state.routing or RoutingState()
        if detection.template_version_id is not None:
            routing.chosen_template_version_id = detection.template_version_id
            routing.stage1_filter_source = Stage1FilterSource.CODE_WORD
            routing.stage1_candidate_count = 1
            routing.confidence = detection.confidence
        state.routing = routing

        warnings: list[str] = []
        if not detection.carrier_phrase_heard:
            # CODEWORD_COMPLIANCE. Worth saying plainly in the trace, because
            # the fix is a conversation with the radiologist, not a code change.
            warnings.append("no carrier phrase in the opening window: CODEWORD_COMPLIANCE miss (behaviour), routing falls through to the cascade")
        elif detection.spoken_code_heard is None:
            warnings.append("carrier phrase heard but no study code matched: STUDYCODE_RECALL miss (engine) — check keyterm biasing before blaming the convention")
        elif detection.escalated:
            warnings.append(f"study code {detection.spoken_code_heard!r} is within {detection.margin:.3f} of another code; refusing to route on it")

        log.info("study_code_detection", carrier_phrase_heard=detection.carrier_phrase_heard, spoken_code_heard=detection.spoken_code_heard, escalated=detection.escalated, margin=detection.margin, tenant_id=str(state.tenant_id))
        return StageResult(output=state, confidence=detection.confidence, warnings=warnings)

    def _window_text(self, state: PipelineState) -> str:
        """The opening window, by audio time where utterances exist."""
        assert state.transcript is not None
        utterances = [u for u in state.utterances if u.audio_start_ms < self._window_ms]
        if utterances:
            end = max(u.char_end for u in utterances)
            return state.transcript.text[:end]

        meta = state.audio_meta
        if meta and meta.duration_seconds > 0:
            fraction = min(1.0, (self._window_ms / 1000) / meta.duration_seconds)
            return state.transcript.text[: max(40, int(len(state.transcript.text) * fraction))]
        return state.transcript.text[:400]


def detect_study_code(window_text: str, entries: tuple[StudyCodeEntry, ...], *, tau_margin: float = TAU_MARGIN, max_distance: float = 0.20) -> StudyCodeDetection:
    """Find the carrier phrase, then the code behind it. Pure and reproducible."""
    detection = StudyCodeDetection()

    carrier = _CARRIER.search(window_text)
    detection.carrier_phrase_heard = carrier is not None
    if carrier is None or not entries:
        return detection

    tail = window_text[carrier.end() : carrier.end() + _CODE_LOOKAHEAD_CHARS].strip()
    if not tail:
        return detection

    match = _best_code(tail, entries, max_distance=max_distance)
    if match is None:
        return detection

    detection.spoken_code_heard = match.surface
    detection.margin = match.margin
    detection.escalated = match.margin < tau_margin

    if detection.escalated:
        # Heard, but refused. The cascade takes over rather than routing
        # on a code that could be either of two templates.
        detection.confidence = 0.0
        return detection

    detection.template_version_id = match.entry.template_version_id
    # Distance 0 is not certainty — it is "this matched exactly". The margin is
    # what separates it from the runner-up, so confidence is built from both.
    detection.confidence = round(max(0.0, min(1.0, (1.0 - match.distance) * min(1.0, match.margin / tau_margin))), 4)
    return detection


def _best_code(tail: str, entries: tuple[StudyCodeEntry, ...], *, max_distance: float) -> CodeMatch | None:
    """Score every code against the leading phrase of `tail`."""
    # Strip punctuation from the probe words: the radiologist said "ct chest plain", and reporting the code as "plain," would make every downstream comparison — and every metric bucket — punctuation-sensitive.
    words = [w.strip(",.;:!?\"'") for w in tail.split()]
    words = [w for w in words if w]
    if not words:
        return None

    scored: list[tuple[StudyCodeEntry, float, str]] = []
    for entry in entries:
        for surface in (entry.spoken_study_code, *entry.variants):
            if not surface:
                continue
            width = max(1, len(surface.split()))
            probe = " ".join(words[:width])
            scored.append((entry, phonetic_distance(probe, surface), probe))

    if not scored:
        return None
    scored.sort(key=lambda row: (row[1], row[0].template_code))

    best_entry, best_distance, best_probe = scored[0]
    if best_distance > max_distance:
        return None

    # The runner-up must be a *different template*.
    runner_up = next((d for entry, d, _ in scored if entry.template_version_id != best_entry.template_version_id), 1.0)
    return CodeMatch(entry=best_entry, distance=best_distance, margin=round(runner_up - best_distance, 4), surface=best_probe)


def compliance_metrics(detections: list[StudyCodeDetection]) -> dict[str, float]:
    """The two rates, computed the way they must be reported."""
    total = len(detections)
    if not total:
        return {"CODEWORD_COMPLIANCE": 0.0, "STUDYCODE_RECALL": 0.0, "n": 0}

    compliant = [d for d in detections if d.carrier_phrase_heard]
    heard = [d for d in compliant if d.spoken_code_heard is not None and not d.escalated]
    return {"CODEWORD_COMPLIANCE": round(len(compliant) / total, 4), "STUDYCODE_RECALL": round(len(heard) / len(compliant), 4) if compliant else 0.0, "n": total, "n_compliant": len(compliant)}
