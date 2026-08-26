"""Stage 2: turns the audio into text, telling the engine which of the lab's terms to expect.

Order: build the list of terms to bias toward (build_keyterms), then run the engine (AsrStage).
Which engine runs is configured elsewhere, not chosen here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from radreport.adapters.asr.base import ASRConfig, ASREngine, ASRResult
from radreport.adapters.storage.object_store import ObjectStore
from radreport.core.logging import get_logger
from radreport.core.types import AsrStatus, TranscriptStage
from radreport.db.models.asr import AsrRun
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.stages.providers import KnowledgeProvider, TenantKnowledge
from radreport.pipeline.state import AsrRunRef, PipelineState, TranscriptState, WordTimingState
from radreport.pipeline.timing import build_timing_map, coverage

log = get_logger(__name__)

#: Engines cap their biasing lists, and a list long enough to include every
#: mined term biases toward noise. Ranked by observation count, truncated here.
MAX_KEYTERMS = 500


@dataclass(frozen=True, slots=True)
class AsrStageConfig:
    language: str = "en-IN"
    diarize: bool = False
    punctuate: bool = True
    model_variant: str | None = None


def build_keyterms(knowledge: TenantKnowledge, *, limit: int = MAX_KEYTERMS) -> tuple[str, ...]:
    """The biasing list: canonical forms plus the variants verbatim annotation actually observed."""
    terms: list[str] = []
    for entry in knowledge.lexicon:
        terms.append(entry.canonical_form)
        if entry.short_form:
            terms.append(entry.short_form)
        terms.extend(entry.surface_variants)
    for code in knowledge.study_codes:
        terms.append(code.spoken_study_code)
        terms.extend(code.variants)

    seen: set[str] = set()
    ordered: list[str] = []
    for term in terms:
        cleaned = term.strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            ordered.append(cleaned)
    return tuple(sorted(ordered)[:limit])


class AsrStage:
    """Calls a provider, writes nothing directly."""

    name = "asr"
    version = "1.0.0"

    def __init__(self, engine: ASREngine, store: ObjectStore, knowledge: KnowledgeProvider, *, config: AsrStageConfig | None = None) -> None:
        self._engine = engine
        self._store = store
        self._knowledge = knowledge
        self._config = config or AsrStageConfig()

    def is_idempotent(self) -> bool:
        """False: this calls a paid provider."""
        return False

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if not state.audio_object_key:
            raise ValueError("no audio_object_key on the pipeline state")

        knowledge = self._knowledge.for_tenant(state.tenant_id)
        keyterms = build_keyterms(knowledge) if self._engine.supports_keyterm_biasing() else ()
        if keyterms and not self._engine.supports_keyterm_biasing():  # pragma: no cover
            keyterms = ()

        config = ASRConfig(keyterms=keyterms, language=self._config.language, diarize=self._config.diarize, punctuate=self._config.punctuate, model_variant=self._config.model_variant)

        audio = self._store.get(state.audio_object_key)
        result: ASRResult = await self._engine.transcribe(audio, config)

        asr_run = AsrRun(
            # Assigned here rather than left to the column's server default: the state needs to reference this run *now*, and the orchestrator does not flush until the stage has already returned.
            id=uuid.uuid4(),
            tenant_id=state.tenant_id,
            recording_id=state.recording_id,
            engine=self._engine.engine,
            engine_version=self._engine.engine_version,
            config_hash=config.config_hash(),
            # the full vendor payload is retained. Reprocessing an
            # archive is far cheaper than re-transcribing it.
            raw_response=result.raw or None,
            overall_confidence=result.overall_confidence,
            latency_ms=result.latency_ms,
            status=AsrStatus.OK,
        )

        # The word timings, carried forward rather than discarded.
        state.word_timings = [WordTimingState(char_start=t.char_start, char_end=t.char_end, start_ms=t.start_ms, end_ms=t.end_ms) for t in build_timing_map(result.text, result.words)]

        state.transcript = TranscriptState(
            version=1,
            stage=TranscriptStage.RAW,
            text=result.text,
            # Single-engine at V1. `disagreement_score` stays null until Beta's
            # multi-engine fan-out gives it something to measure.
            reconciliation_method="single",
        )
        state.asr_runs = [AsrRunRef(asr_run_id=asr_run.id, engine=self._engine.engine, engine_version=self._engine.engine_version, overall_confidence=result.overall_confidence)]

        warnings: list[str] = []
        if not self._engine.supports_keyterm_biasing():
            warnings.append(f"{self._engine.engine} does not support keyterm biasing; CODEWORD_RECALL will be lower than the bake-off measured with it")
        if not result.text.strip():
            warnings.append("engine returned an empty transcript")

        log.info("asr_complete", engine=self._engine.engine, engine_version=self._engine.engine_version, keyterms=len(keyterms), chars=len(result.text), words=len(result.words), timing_coverage=coverage(build_timing_map(result.text, result.words), result.text), latency_ms=result.latency_ms)
        return StageResult(output=state, confidence=result.overall_confidence or 0.0, duration_ms=result.latency_ms, model_id=self._engine.engine, model_version=self._engine.engine_version, warnings=warnings, pending_writes=[asr_run])
