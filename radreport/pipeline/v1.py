"""The running order of the pipeline: which stage follows which, and which ones may fail without failing the report.

Order: build_v1_graph assembles all 15 stages into the graph that graph.py then runs.
"""

from __future__ import annotations

from collections.abc import Sequence

from radreport.adapters.asr.base import ASREngine
from radreport.adapters.llm.base import LLMClient
from radreport.adapters.storage.object_store import ObjectStore
from radreport.core.types import TaskKey
from radreport.pipeline.graph import PipelineGraph, StageSpec
from radreport.pipeline.stages.asr import AsrStage
from radreport.pipeline.stages.compose import ComposeStage, RenderSpec
from radreport.pipeline.stages.confidence import ConfidenceStage
from radreport.pipeline.stages.critic import CriticStage
from radreport.pipeline.stages.critical import CriticalFindingsStage
from radreport.pipeline.stages.extract import ExtractStage, SectionSpec
from radreport.pipeline.stages.grounding import GroundingStage
from radreport.pipeline.stages.normalise import NormaliseStage
from radreport.pipeline.stages.persist import PersistDraftStage
from radreport.pipeline.stages.post_correction import PostCorrectionStage
from radreport.pipeline.stages.preprocess import PreprocessStage
from radreport.pipeline.stages.providers import KnowledgeProvider
from radreport.pipeline.stages.reconcile import EngineSpec, ReconcileStage
from radreport.pipeline.stages.release import AutonomousReleaseStage
from radreport.pipeline.stages.repairs import ResolveRepairsStage
from radreport.pipeline.stages.route_human import RouteToHumanStage
from radreport.pipeline.stages.routing import RoutingStage, ShortlistPicker, TemplateCandidate
from radreport.pipeline.stages.segment import SegmentStage
from radreport.pipeline.stages.sketch import SketchStage
from radreport.pipeline.stages.study_code import StudyCodeStage
from radreport.pipeline.stages.verify import VerifyStage

#: Bumped whenever the stage list or any stage version changes.
V1_PIPELINE_VERSION = "1.0.0-v1"

#: Beta adds reconciliation, post-correction and the critic.
BETA_PIPELINE_VERSION = "1.1.0-beta"


def build_v1_graph(*, store: ObjectStore, asr_engine: ASREngine, knowledge: KnowledgeProvider, templates: Sequence[TemplateCandidate], sections: Sequence[SectionSpec], llm_client: LLMClient | None = None, shortlist_picker: ShortlistPicker | None = None, extra_asr_engines: Sequence[EngineSpec] = (), enable_post_correction: bool = False, enable_critic: bool = False, render_spec: RenderSpec | None = None, critical_field_keys: frozenset[str] = frozenset(), enum_options: dict[str, tuple[str, ...]] | None = None, referrer_prior: dict[str, float] | None = None, weeks_since_go_live: int = 0, enable_autonomous_release: bool = False, version: str = V1_PIPELINE_VERSION) -> PipelineGraph:
    """Assemble the 15-stage V1 graph."""
    # The multi-engine path replaces the single-engine stage rather than wrapping it: a two-engine "fan-out" with one engine is just the V1 stage with extra machinery and an extra name in the trace.
    if extra_asr_engines:
        asr_spec = StageSpec(ReconcileStage([EngineSpec(engine=asr_engine), *extra_asr_engines], store, knowledge, llm_client=llm_client))
    else:
        asr_spec = StageSpec(AsrStage(asr_engine, store, knowledge))

    specs: list[StageSpec] = [StageSpec(PreprocessStage(store)), asr_spec]

    if enable_post_correction:
        # Immediately after ASR and **before** anything records a character offset.
        specs.append(StageSpec(PostCorrectionStage(knowledge)))

    specs += [
        StageSpec(NormaliseStage(knowledge)),
        StageSpec(StudyCodeStage(knowledge)),
        StageSpec(SegmentStage(llm_client), task_key=TaskKey.UTTERANCE_CLASSIFICATION if llm_client else None),
        StageSpec(ResolveRepairsStage()),
        # Never optional.: a report that reached a queue with its alerting
        # silently skipped looks exactly like one with no critical findings.
        StageSpec(CriticalFindingsStage(knowledge)),
        StageSpec(SketchStage(llm_client), task_key=TaskKey.EXTRACTION if llm_client else None),
        StageSpec(RoutingStage(knowledge, templates, picker=shortlist_picker, referrer_prior=referrer_prior), task_key=TaskKey.ROUTING_PICK if shortlist_picker else None),
    ]

    if llm_client is not None and sections:
        specs.append(StageSpec(ExtractStage(llm_client, list(sections)), task_key=TaskKey.EXTRACTION))

    specs += [
        # Never optional. A draft whose provenance was never checked is
        # indistinguishable from a grounded one (I1).
        StageSpec(GroundingStage()),
        StageSpec(ComposeStage(render_spec)),
        StageSpec(VerifyStage(enum_options=enum_options)),
    ]

    if enable_critic and llm_client is not None:
        # `optional`, unlike grounding and critical findings: the deterministic checks have already run and they carry the safety argument, so a critic that fails to respond must not fail the report.
        specs.append(StageSpec(CriticStage(llm_client), task_key=TaskKey.VERIFICATION, optional=True))

    specs += [
        StageSpec(ConfidenceStage(critical_field_keys=critical_field_keys)),
        StageSpec(
            RouteToHumanStage(
                critical_field_keys=critical_field_keys,
                weeks_since_go_live=weeks_since_go_live,
                # Passing the provider is what makes autonomy consulted at all.
                knowledge=knowledge if enable_autonomous_release else None,
            )
        ),
        # The seam to the review surface.
        StageSpec(PersistDraftStage(knowledge)),
    ]

    if enable_autonomous_release:
        # Last, and after persistence: it files a `final_report` against the draft stage 16 built.
        specs.append(StageSpec(AutonomousReleaseStage(knowledge)))

    return PipelineGraph(specs, version=version)


#: Plan the numbering, for cross-referencing the design doc against the code.
STAGE_NUMBERS: dict[str, int] = {
    "preprocess": 1,
    "asr": 2,
    "asr_reconcile": 2,
    "post_correction": 2,
    "normalise": 3,
    "study_code": 4,
    "segment_classify": 5,
    "resolve_repairs": 6,
    "critical_findings": 7,
    "finding_sketch": 8,
    "routing": 9,
    "extract": 10,
    "grounding": 11,
    "compose": 12,
    "verify": 13,
    "llm_critic": 13,
    "confidence": 14,
    "route_to_human": 15,
    # Not in the list: stops at the last *decision*, and these are
    # what make the decision reachable and, at GA, act on it.
    "persist_draft": 16,
    "autonomous_release": 17,
}
