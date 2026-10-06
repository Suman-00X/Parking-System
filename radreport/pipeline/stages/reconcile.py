"""Stage 2b: runs several speech engines on the same audio and votes word by word where they disagree.

Order: ReconcileStage fans the audio out to each configured engine (EngineSpec) and merges the
results. It replaces the single-engine stage whenever more than one engine is configured.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass

from radreport.adapters.asr.base import ASRConfig, ASREngine, ASRResult
from radreport.adapters.asr.rover import DISPUTE_MARGIN, EngineHypothesis, RoverResult, apply_arbitration, arbitration_payload, reconcile
from radreport.adapters.llm.base import LLMClient, LLMRequest
from radreport.adapters.llm.prompt import PromptBundle, VolatileBlock, system_block
from radreport.adapters.storage.object_store import ObjectStore
from radreport.core.logging import get_logger
from radreport.core.types import AsrStatus, ReconciliationMethod, TaskKey, TranscriptStage
from radreport.db.models.asr import AsrRun
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.stages.asr import build_keyterms
from radreport.pipeline.stages.providers import KnowledgeProvider
from radreport.pipeline.state import AsrRunRef, PipelineState, TranscriptState, WordTimingState
from radreport.pipeline.timing import build_timing_map

log = get_logger(__name__)

ARBITRATION_PROMPT = """\
You adjudicate disagreements between speech-recognition engines on a dictated
radiology report.

For each disputed position you are given the candidates the engines produced,
with the surrounding words. Choose the candidate that makes clinical and
grammatical sense in context.

Rules:
- Choose ONLY from the candidates offered. You are settling a disagreement, not
  transcribing.
- "<silence>" is a valid choice: it means no word was spoken there.
- Prefer the clinically coherent reading. "no pneumothorax" and "a
  pneumothorax" are both grammatical; the surrounding sentence decides.
- If the candidates are clinically equivalent, choose the more common spelling.

Return JSON: {"decisions": [{"slot_index": int, "token": str}]}"""


@dataclass(frozen=True, slots=True)
class EngineSpec:
    """One engine in the fan-out, with its bake-off weight."""

    engine: ASREngine
    weight: float = 1.0
    """From the engine bake-off. An engine that scored materially worse on *your* audio should not carry an equal vote."""


class ReconcileStage:
    """N transcriptions, one vote, bounded arbitration."""

    name = "asr_reconcile"
    version = "1.0.0"
    task_key = TaskKey.UTTERANCE_CLASSIFICATION

    def __init__(self, engines: list[EngineSpec], store: ObjectStore, knowledge: KnowledgeProvider, *, llm_client: LLMClient | None = None, config: ASRConfig | None = None, dispute_margin: float = DISPUTE_MARGIN) -> None:
        if not engines:
            raise ValueError("reconciliation needs at least one engine")
        self._engines = engines
        self._store = store
        self._knowledge = knowledge
        self._llm = llm_client
        self._config = config
        self._dispute_margin = dispute_margin

    def is_idempotent(self) -> bool:
        """False: N paid transcriptions plus a possible arbitration call."""
        return False

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if not state.audio_object_key:
            raise ValueError("no audio_object_key on the pipeline state")

        knowledge = self._knowledge.for_tenant(state.tenant_id)
        keyterms = build_keyterms(knowledge)
        audio = self._store.get(state.audio_object_key)

        outcomes = await asyncio.gather(*(self._transcribe(spec, audio, keyterms) for spec in self._engines), return_exceptions=False)

        warnings: list[str] = []
        hypotheses: list[EngineHypothesis] = []
        runs: list[AsrRun] = []

        for spec, result, error in outcomes:
            if error is not None:
                # Degrading the vote beats failing the report.
                warnings.append(f"{spec.engine.engine} failed: {error}")
                runs.append(self._failed_run(state, spec, error))
                continue
            assert result is not None, "an engine that raised no error returned a result"
            hypotheses.append(EngineHypothesis(engine=spec.engine.engine, engine_version=spec.engine.engine_version, text=result.text, words=tuple(result.words), weight=spec.weight))
            runs.append(self._ok_run(state, spec, result, keyterms))

        if not hypotheses:
            raise ValueError("every ASR engine failed; a report with no transcript is not a degraded report")

        rover = reconcile(hypotheses, dispute_margin=self._dispute_margin)

        cost = 0.0
        model_id: str | None = None
        method = ReconciliationMethod.SINGLE if len(hypotheses) == 1 else ReconciliationMethod.ROVER

        if self._llm is not None and rover.disputed and len(hypotheses) > 1:
            resolved, cost, model_id = await self._arbitrate(rover, ctx)
            if resolved:
                apply_arbitration(rover, resolved)
                method = ReconciliationMethod.LLM_ARBITRATED

        # Timings from the most complete hypothesis.
        best = max(hypotheses, key=lambda h: len(h.words))
        state.word_timings = [WordTimingState(char_start=t.char_start, char_end=t.char_end, start_ms=t.start_ms, end_ms=t.end_ms) for t in build_timing_map(rover.text, list(best.words))]

        state.transcript = TranscriptState(
            version=1,
            stage=TranscriptStage.RECONCILED,
            text=rover.text,
            reconciliation_method=method,
            # Null with a single engine, which has nothing to disagree
            # with. This is the moment it becomes a real signal.
            disagreement_score=rover.disagreement_score,
        )
        state.asr_runs = [AsrRunRef(asr_run_id=run.id, engine=run.engine, engine_version=run.engine_version, overall_confidence=run.overall_confidence) for run in runs if run.status == AsrStatus.OK]

        if rover.dispute_rate > 0.10:
            warnings.append(f"engines disagreed on {rover.dispute_rate:.0%} of slots — high disagreement usually means difficult audio rather than a bad engine")

        log.info("asr_reconciled", engines=[h.engine for h in hypotheses], failed=len(self._engines) - len(hypotheses), method=method, disagreement_score=rover.disagreement_score, disputed=len(rover.disputed), arbitrated=method == ReconciliationMethod.LLM_ARBITRATED)
        return StageResult(output=state, confidence=round(1.0 - rover.disagreement_score, 4), cost_usd=cost, model_id=model_id, warnings=warnings, pending_writes=runs)

    async def _transcribe(self, spec: EngineSpec, audio: bytes, keyterms: tuple[str, ...]) -> tuple[EngineSpec, ASRResult | None, str | None]:
        config = self._config or ASRConfig(keyterms=keyterms if spec.engine.supports_keyterm_biasing() else ())
        try:
            return spec, await spec.engine.transcribe(audio, config), None
        except Exception as exc:  # noqa: BLE001 - one engine must not fail the run
            return spec, None, f"{type(exc).__name__}: {exc}"

    def _ok_run(self, state: PipelineState, spec: EngineSpec, result: ASRResult, keyterms: tuple[str, ...]) -> AsrRun:
        config = self._config or ASRConfig(keyterms=keyterms)
        return AsrRun(id=uuid.uuid4(), tenant_id=state.tenant_id, recording_id=state.recording_id, engine=spec.engine.engine, engine_version=spec.engine.engine_version, config_hash=config.config_hash(), raw_response=result.raw or None, overall_confidence=result.overall_confidence, latency_ms=result.latency_ms, status=AsrStatus.OK)

    def _failed_run(self, state: PipelineState, spec: EngineSpec, error: str) -> AsrRun:
        """A failed engine still gets a row."""
        return AsrRun(id=uuid.uuid4(), tenant_id=state.tenant_id, recording_id=state.recording_id, engine=spec.engine.engine, engine_version=spec.engine.engine_version, config_hash="failed", raw_response={"error": error}, status=AsrStatus.FAILED)

    async def _arbitrate(self, rover: RoverResult, ctx: StageContext) -> tuple[dict[int, str], float, str | None]:
        """Send only the disputed spans to the model."""
        payload = arbitration_payload(rover)
        if not payload:
            return {}, 0.0, None

        resolved = await ctx.resolve_model(TaskKey.SELF_CORRECTION)
        model_id = resolved.ref.model_id
        prompt = PromptBundle(stable=[system_block(ARBITRATION_PROMPT)], volatile=[VolatileBlock(text=json.dumps({"disputes": payload}, separators=(",", ":")), label="disputed_spans")])
        assert self._llm is not None, "arbitration runs only when an LLM client was given"
        response = await self._llm.complete(LLMRequest(prompt=prompt, max_tokens=2048), model_id=model_id)

        offered = {span.slot_index for span in rover.disputed}
        candidates = {span.slot_index: {token for token, _score in span.candidates} for span in rover.disputed}
        try:
            decisions = json.loads(response.text).get("decisions", [])
        except json.JSONDecodeError:
            log.warning("arbitration_response_not_json")
            return {}, response.cost_usd, model_id

        chosen: dict[int, str] = {}
        for item in decisions:
            index = item.get("slot_index")
            token = item.get("token", "")
            token = "" if token == "<silence>" else token
            if index not in offered:
                continue
            if token not in candidates.get(index, set()):
                # The arbitrator was settling a disagreement, not transcribing.
                # A token nobody offered is a new hypothesis with one vote.
                log.warning("arbitration_token_off_ballot", slot_index=index, token=token)
                continue
            chosen[index] = token

        return chosen, response.cost_usd, model_id
