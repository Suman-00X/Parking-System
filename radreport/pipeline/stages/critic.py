"""Stage 13b: a second model reads the draft back and reports anything the fixed rules could not catch.

Order: parse the critic's findings (parse_critic) and its round-trip check that the text still
means what the fields say (parse_entailment). Marked optional, so a critic that fails does not
fail the report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from radreport.adapters.llm.base import LLMClient, LLMRequest
from radreport.adapters.llm.prompt import PromptBundle, VolatileBlock, section_block, system_block
from radreport.core.logging import get_logger
from radreport.core.types import CheckType, Severity, TaskKey
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.stages.grounding import renderable
from radreport.pipeline.state import PipelineState, VerificationFindingState

log = get_logger(__name__)

CRITIC_PROMPT = """\
You review a draft radiology report against the transcript it was built from,
and report contradictions.

Report a finding when the draft says something the transcript does not support,
or contradicts. Do NOT report style, phrasing, completeness or clinical
judgement — only disagreements with the source.

You are tuned for RECALL. If you are unsure whether something is a
contradiction, report it. A missed contradiction reaches a patient; a false one
costs a reviewer a glance.

Severity:
- "block": the draft asserts something the transcript contradicts.
- "error": the draft asserts something the transcript does not support.
- "warn": the draft may overstate or understate what was said.

Return JSON: {"findings": [{"field_key": str|null, "severity": str,
"message": str, "evidence": str}]}"""

ENTAILMENT_PROMPT = """\
You check whether a transcript ENTAILS a report.

You are given a dictated transcript and a report rendered from it. For each
sentence of the report, decide whether the transcript supports it.

- "entailed": the transcript says this.
- "unsupported": the transcript neither says nor contradicts this.
- "contradicted": the transcript says otherwise.

Judge only against the transcript. Do not use outside clinical knowledge: a
statement that is medically true but was never dictated is `unsupported`, and
that is exactly what this check exists to find.

Return JSON: {"sentences": [{"text": str, "verdict": str, "evidence": str}]}"""


@dataclass(frozen=True, slots=True)
class CriticFinding:
    field_key: str | None
    severity: str
    message: str
    evidence: str


@dataclass(slots=True)
class CriticResult:
    findings: list[CriticFinding] = field(default_factory=list)
    unsupported_sentences: list[str] = field(default_factory=list)
    contradicted_sentences: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    model_id: str | None = None

    @property
    def entailment_rate(self) -> float:
        """Share of report sentences the transcript actually supports."""
        total = len(self.unsupported_sentences) + len(self.contradicted_sentences)
        return 0.0 if total else 1.0


def parse_critic(text: str) -> list[CriticFinding]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        log.warning("critic_response_not_json", length=len(text))
        return []

    findings: list[CriticFinding] = []
    for item in payload.get("findings", []):
        severity = item.get("severity", Severity.WARN)
        if severity not in Severity.values():
            severity = Severity.WARN
        message = str(item.get("message", "")).strip()
        if not message:
            continue
        findings.append(CriticFinding(field_key=item.get("field_key") or None, severity=severity, message=message, evidence=str(item.get("evidence", ""))))
    return findings


def parse_entailment(text: str) -> tuple[list[str], list[str]]:
    """`(unsupported, contradicted)` sentences."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        log.warning("entailment_response_not_json", length=len(text))
        return [], []

    unsupported: list[str] = []
    contradicted: list[str] = []
    for item in payload.get("sentences", []):
        verdict = item.get("verdict")
        sentence = str(item.get("text", "")).strip()
        if not sentence:
            continue
        if verdict == "unsupported":
            unsupported.append(sentence)
        elif verdict == "contradicted":
            contradicted.append(sentence)
    return unsupported, contradicted


class CriticStage:
    """Raises findings; never edits a value."""

    name = "llm_critic"
    version = "1.0.0"
    task_key = TaskKey.VERIFICATION

    def __init__(self, client: LLMClient, *, run_entailment: bool = True, max_tokens: int = 2048) -> None:
        self._client = client
        self._run_entailment = run_entailment
        self._max_tokens = max_tokens

    def is_idempotent(self) -> bool:
        return False

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if state.transcript is None:
            raise ValueError("the critic runs against a transcript; none is present")

        resolved = await ctx.resolve_model(self.task_key)
        model_id = resolved.ref.model_id
        result = CriticResult(model_id=model_id)

        # Only grounded values are shown.
        values = renderable(state)
        summary = {key: {"value": value.value_text or value.value_enum, "assertion": value.assertion_status, "laterality": value.laterality} for key, value in values.items()}

        critic_response = await self._client.complete(LLMRequest(prompt=PromptBundle(stable=[system_block(CRITIC_PROMPT)], volatile=[VolatileBlock(text=(f"<transcript>\n{state.transcript.text}\n</transcript>\n<draft>\n{json.dumps(summary, sort_keys=True)}\n</draft>"), label="draft_and_transcript")]), max_tokens=self._max_tokens), model_id=model_id)
        result.cost_usd += critic_response.cost_usd
        result.findings = parse_critic(critic_response.text)

        if self._run_entailment and state.rendered_text:
            entailment_response = await self._client.complete(LLMRequest(prompt=PromptBundle(stable=[system_block(ENTAILMENT_PROMPT), section_block("Judge each report sentence independently.", label="entailment_rules")], volatile=[VolatileBlock(text=(f"<transcript>\n{state.transcript.text}\n</transcript>\n<report>\n{state.rendered_text}\n</report>"), label="report_and_transcript")]), max_tokens=self._max_tokens), model_id=model_id)
            result.cost_usd += entailment_response.cost_usd
            unsupported, contradicted = parse_entailment(entailment_response.text)
            result.unsupported_sentences = unsupported
            result.contradicted_sentences = contradicted

        state.verification.extend(_to_state_findings(result))

        warnings = [f"{f.severity}: {f.message}" for f in result.findings]
        if result.contradicted_sentences:
            warnings.append(f"{len(result.contradicted_sentences)} report sentence(s) contradicted by the transcript")

        log.info("critic_complete", findings=len(result.findings), unsupported=len(result.unsupported_sentences), contradicted=len(result.contradicted_sentences), cost_usd=round(result.cost_usd, 6))
        return StageResult(output=state, confidence=result.entailment_rate, cost_usd=result.cost_usd, model_id=model_id, warnings=warnings)


def _to_state_findings(result: CriticResult) -> list[VerificationFindingState]:
    """Critic output as verification findings, kept distinguishable."""
    findings = [VerificationFindingState(check_id="llm_critic", check_type=CheckType.LLM_CRITIC, severity=f.severity, message=f.message, field_key=f.field_key, evidence={"quote": f.evidence}) for f in result.findings]
    findings += [VerificationFindingState(check_id="roundtrip_contradicted", check_type=CheckType.ROUNDTRIP, severity=Severity.BLOCK, message=f"the transcript contradicts: {sentence!r}", evidence={"sentence": sentence}) for sentence in result.contradicted_sentences]
    findings += [
        VerificationFindingState(
            check_id="roundtrip_unsupported",
            check_type=CheckType.ROUNDTRIP,
            # `error`, not `block`: an unsupported sentence is often a rendering artefact rather than an invention, and blocking on every one would stop more reports than it should.
            severity=Severity.ERROR,
            message=f"nothing in the transcript supports: {sentence!r}",
            evidence={"sentence": sentence},
        )
        for sentence in result.unsupported_sentences
    ]
    return findings
