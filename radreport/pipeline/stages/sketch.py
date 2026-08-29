"""Stage 8: writes down the findings in the dictation before any template is chosen, so routing has something to check against.

Order: build the prompt (build_prompt) -> read the answer (parse_response), with a plain
fallback if the model fails (fallback_sketch).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from radreport.adapters.llm.base import LLMClient, LLMRequest
from radreport.adapters.llm.prompt import PromptBundle, VolatileBlock, section_block, system_block
from radreport.core.logging import get_logger
from radreport.core.text import split_sentences
from radreport.core.types import TaskKey
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.state import FindingSketch, PipelineState

log = get_logger(__name__)

SYSTEM_PROMPT = """\
You list the clinical assertions in a dictated radiology report.

You have no template. Do not organise, normalise or interpret — list what was
said, in the order it was said.

Rules:
- One assertion per finding, in the radiologist's own words where possible.
- Include negative findings ("no pleural effusion"): a dictated negative is an
  assertion.
- Do not include technique, patient identifiers, or dictation commands.
- List measurements separately, with their units as spoken.

Return JSON: {"assertions": [str], "measurements": [{"text": str,
"value": number, "unit": str}]}"""

SKETCH_INSTRUCTION = """\
Be exhaustive rather than selective. A finding you omit here cannot be checked
against the chosen template later."""

_MEASUREMENT = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|cm|ml|cc|hu)\b", re.IGNORECASE)
#: Sentences that assert something clinical, for the no-model fallback.
_CLINICAL_CUE = re.compile(
    r"\b(no|normal|enlarged|dilated|lesion|mass|cyst|nodule|effusion|"
    r"calculus|stone|thickening|oedema|edema|hernia|fracture|opacity|"
    r"consolidation|unremarkable|measuring|measures)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SketchResult:
    sketch: FindingSketch
    used_model: bool


def build_prompt(transcript: str) -> PromptBundle:
    return PromptBundle(stable=[system_block(SYSTEM_PROMPT), section_block(SKETCH_INSTRUCTION, label="sketch_rules")], volatile=[VolatileBlock(text=f"<transcript>\n{transcript}\n</transcript>", label="transcript")])


def fallback_sketch(transcript: str, included_text: str | None = None) -> FindingSketch:
    """Cue-based assertion listing, for when no model is bound."""
    source = included_text if included_text is not None else transcript
    assertions: list[str] = []
    measurements: list[dict[str, object]] = []

    for sentence in split_sentences(source):
        stripped = sentence.strip()
        if not stripped or not _CLINICAL_CUE.search(stripped):
            continue
        assertions.append(stripped)
        for match in _MEASUREMENT.finditer(stripped):
            measurements.append({"text": match.group(0), "value": float(match.group(1)), "unit": match.group(2).lower()})

    return FindingSketch(assertions=assertions, measurements=measurements)


def parse_response(text: str) -> FindingSketch | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        log.warning("sketch_response_not_json", length=len(text))
        return None

    assertions = [str(a).strip() for a in payload.get("assertions", []) if str(a).strip()]
    measurements = [m for m in payload.get("measurements", []) if isinstance(m, dict)]
    if not assertions:
        return None
    return FindingSketch(assertions=assertions, measurements=measurements)


class SketchStage:
    """Template-free; runs before routing."""

    name = "finding_sketch"
    version = "1.0.0"
    task_key = TaskKey.EXTRACTION

    def __init__(self, client: LLMClient | None = None, *, max_tokens: int = 2048) -> None:
        self._client = client
        self._max_tokens = max_tokens

    def is_idempotent(self) -> bool:
        return self._client is None

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if state.transcript is None:
            raise ValueError("the finding sketch runs on a transcript; none is present")

        # Only included spans: a retracted self-correction or another speaker's remark must not become an assertion the router is then measured against.
        included = " ".join(u.text for u in state.included_utterances())
        source = included or state.transcript.text

        cost = 0.0
        model_id: str | None = None
        sketch: FindingSketch | None = None

        if self._client is not None:
            resolved = await ctx.resolve_model(self.task_key)
            model_id = resolved.ref.model_id
            response = await self._client.complete(LLMRequest(prompt=build_prompt(source), max_tokens=self._max_tokens), model_id=model_id)
            cost = response.cost_usd
            sketch = parse_response(response.text)

        warnings: list[str] = []
        used_model = sketch is not None
        if sketch is None:
            sketch = fallback_sketch(state.transcript.text, included_text=source)
            warnings.append("finding sketch fell back to cue matching; it over-lists rather than under-lists, so expect false orphan-assertion signals")

        state.sketch = sketch
        log.info("sketch_complete", assertions=len(sketch.assertions), measurements=len(sketch.measurements), used_model=used_model)
        return StageResult(output=state, confidence=1.0 if used_model else 0.5, cost_usd=cost, model_id=model_id, warnings=warnings)
