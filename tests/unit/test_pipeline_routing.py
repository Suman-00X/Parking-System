"""Recognising the speech, choosing the template, composing the text, and deciding who reviews it."""

from __future__ import annotations

import uuid

import pytest

from radreport.core.types import AssertionStatus, CheckType, FillSource, PathType, ReviewerRole, Severity, Sex, Stage1FilterSource, StudyPriority, TermType
from radreport.pipeline.stages.asr import build_keyterms
from radreport.pipeline.stages.compose import ComposeStage, RenderSpec, compose
from radreport.pipeline.stages.providers import LexiconEntry, StaticKnowledgeProvider, StudyCodeEntry, TenantKnowledge
from radreport.pipeline.stages.route_human import decide, grading_rate, is_sampled_for_grading
from radreport.pipeline.stages.routing import MIN_ROUTING_CONFIDENCE, PatientContext, RoutingStage, TemplateCandidate, demographic_filter, hard_filter, hybrid_rank
from radreport.pipeline.state import FieldValue, PipelineState, StudyCodeDetection, TranscriptState, VerificationFindingState

TENANT = uuid.uuid4()


def _state(text: str = "", **kw) -> PipelineState:
    return PipelineState(tenant_id=TENANT, recording_id=kw.pop("recording_id", uuid.uuid4()), pipeline_run_id=uuid.uuid4(), transcript=TranscriptState(text=text) if text else None, **kw)


def _candidate(code: str, **kw) -> TemplateCandidate:
    return TemplateCandidate(template_version_id=kw.pop("version_id", uuid.uuid4()), template_code=code, display_name=kw.pop("display_name", code.replace("_", " ").title()), modality=kw.pop("modality", "CT"), body_region=kw.pop("body_region", "chest"), routing_card=kw.pop("routing_card", f"{code} routing card lungs pleura mediastinum"), spoken_study_code=kw.pop("spoken", code.lower().replace("_", " ")), **kw)


# ==================================================== stage 2: keyterms ======
def test_keyterms_include_mined_variants_and_are_order_stable() -> None:
    """The list is hashed into `config_hash`; an unstable order would make every run look like a new configuration and defeat idempotency."""
    knowledge = TenantKnowledge(tenant_id=TENANT, lexicon=(LexiconEntry(canonical_form="echotexture", term_type=TermType.ANATOMY, phonetic_key_primary="AXTKSTR", surface_variants=("echo texture",)),), study_codes=(StudyCodeEntry(template_version_id=uuid.uuid4(), template_code="CT_CHEST", spoken_study_code="ct chest plain", phonetic_key="KXSTPLN"),))
    first = build_keyterms(knowledge)
    assert first == build_keyterms(knowledge)
    assert "echo texture" in first
    assert "ct chest plain" in first


# ================================================ stage 9: route cascade =====
@pytest.mark.asyncio
async def test_a_heard_study_code_routes_without_calling_a_model() -> None:
    """The short-circuit that makes the spoken-code convention worth having."""
    chest = _candidate("CT_CHEST")
    abdo = _candidate("USG_ABDOMEN", modality="US", body_region="abdomen")

    state = _state("study type ct chest plain, the lungs are clear")
    state.study_code = StudyCodeDetection(carrier_phrase_heard=True, spoken_code_heard="ct chest plain", template_version_id=chest.template_version_id, confidence=0.9)

    called = False

    class _Picker:
        async def pick(self, transcript, shortlist):  # pragma: no cover
            nonlocal called
            called = True
            return shortlist[0].candidate.template_version_id, 0.9, "model"

    stage = RoutingStage(StaticKnowledgeProvider(TenantKnowledge(tenant_id=TENANT)), [chest, abdo], picker=_Picker())
    result = await stage.run(state, ctx=object())

    assert called is False, "a heard study code must not reach the model"
    assert result.output.routing.chosen_template_version_id == chest.template_version_id
    assert result.output.routing.stage1_filter_source == Stage1FilterSource.CODE_WORD
    assert [type(w).__name__ for w in result.pending_writes] == ["RoutingDecision"]


@pytest.mark.asyncio
async def test_an_escalated_study_code_falls_through_to_the_cascade() -> None:
    """A code the margin guard refused must not route — that is the point of refusing it."""
    chest = _candidate("CT_CHEST")
    state = _state("study type something ambiguous, lungs pleura mediastinum")
    state.study_code = StudyCodeDetection(carrier_phrase_heard=True, spoken_code_heard="l m p", template_version_id=None, escalated=True)

    stage = RoutingStage(StaticKnowledgeProvider(TenantKnowledge(tenant_id=TENANT)), [chest])
    result = await stage.run(state, ctx=object())

    assert result.output.routing.stage1_filter_source != Stage1FilterSource.CODE_WORD


def test_the_hard_filter_excludes_impossible_modalities() -> None:
    """A CT template cannot serve an ultrasound however well its card matches."""
    ct = _candidate("CT_CHEST", modality="CT")
    us = _candidate("USG_ABDOMEN", modality="US", body_region="abdomen")

    kept = hard_filter([ct, us], PatientContext(modality="US"))
    assert [c.template_code for c in kept] == ["USG_ABDOMEN"]


def test_the_hard_filter_is_skipped_when_metadata_is_absent() -> None:
    """DICOM is deferred behind a measured trigger."""
    ct = _candidate("CT_CHEST")
    us = _candidate("USG_ABDOMEN", modality="US")
    assert len(hard_filter([ct, us], PatientContext())) == 2


def test_demographics_exclude_the_impossible_not_the_unlikely() -> None:
    obstetric = _candidate("USG_OBSTETRIC", applicable_sex=(Sex.F,))
    generic = _candidate("USG_ABDOMEN")

    male = demographic_filter([obstetric, generic], PatientContext(sex=Sex.M))
    assert [c.template_code for c in male] == ["USG_ABDOMEN"]

    # Unknown sex filters nothing — routing on absent data is worse.
    unknown = demographic_filter([obstetric, generic], PatientContext(sex=Sex.U))
    assert len(unknown) == 2


def test_usage_prior_is_sublinear_so_the_head_cannot_swallow_everything() -> None:
    """A template with 10x the volume is more likely, not 10x more likely."""
    common = _candidate("COMMON", usage_count_12m=1000, routing_card="unrelated words here")
    rare = _candidate("RARE", usage_count_12m=10, routing_card="lungs pleura mediastinum nodule")
    ranked = hybrid_rank([common, rare], "the lungs pleura mediastinum show a nodule", PatientContext())

    assert ranked[0].candidate.template_code == "RARE"


@pytest.mark.asyncio
async def test_routing_declines_rather_than_picking_the_nearest_of_twenty() -> None:
    """V1 ships the top ~20 templates, so an out-of-head dictation is expected."""
    unrelated = _candidate("CT_CHEST", routing_card="lungs pleura mediastinum")
    state = _state("the patient tolerated the procedure well")

    stage = RoutingStage(StaticKnowledgeProvider(TenantKnowledge(tenant_id=TENANT)), [unrelated])
    result = await stage.run(state, ctx=object())

    assert result.output.routing.chosen_template_version_id is None
    assert result.output.routing.confidence < MIN_ROUTING_CONFIDENCE
    assert result.pending_writes == []
    assert any("declined" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_a_picker_naming_a_candidate_off_the_shortlist_is_not_followed() -> None:
    """The shortlist was the question; an answer outside it is not an answer."""
    a = _candidate("CT_CHEST", routing_card="lungs pleura mediastinum nodule")
    b = _candidate("CT_ABDOMEN", routing_card="liver spleen kidney pancreas")

    class _RogueP:
        async def pick(self, transcript, shortlist):
            return uuid.uuid4(), 0.99, "a template that was never offered"

    state = _state("lungs pleura mediastinum nodule seen")
    stage = RoutingStage(StaticKnowledgeProvider(TenantKnowledge(tenant_id=TENANT)), [a, b], picker=_RogueP())
    result = await stage.run(state, ctx=object())

    assert result.output.routing.chosen_template_version_id == a.template_version_id


def test_modules_attach_only_when_the_dictation_mentions_them() -> None:
    """Attaching every compatible module adds fields nobody dictated — and an empty field is a prompt for a reviewer to fill it in by hand."""
    from radreport.pipeline.stages.routing import attach_modules

    parent = _candidate("CT_CHEST")
    doppler = _candidate("MOD_DOPPLER", is_module=True, parent_compatible_codes=("CT_CHEST",), display_name="Doppler")
    elasto = _candidate("MOD_ELASTO", is_module=True, parent_compatible_codes=("CT_CHEST",), display_name="Elastography")

    attached = attach_modules(parent, [parent, doppler, elasto], "doppler study performed")
    assert attached == [doppler.template_version_id]


# ==================================================== stage 12: compose ======
SPEC = RenderSpec(sections=("FINDINGS", "IMPRESSION"), field_order=("lungs", "pleura", "summary"), labels={"lungs": "Lungs", "pleura": "Pleura", "summary": "Summary"}, sections_by_field={"lungs": "FINDINGS", "pleura": "FINDINGS", "summary": "IMPRESSION"})


def test_compose_renders_only_grounded_values() -> None:
    """The restriction is a property of the input, not a rule compose keeps."""
    state = _state("x")
    state.field_values = {"lungs": FieldValue(field_key="lungs", value_text="Clear", is_grounded=True), "pleura": FieldValue(field_key="pleura", value_text="Effusion", is_grounded=False)}
    report = compose(state, SPEC)

    assert "Lungs: Clear" in report.text
    assert "Effusion" not in report.text
    assert "pleura" in report.omitted_fields


def test_an_absent_field_is_not_invented() -> None:
    """Nothing auto-fills. A field nobody dictated renders as a gap."""
    state = _state("x")
    state.field_values = {"lungs": FieldValue(field_key="lungs", value_text="Clear", is_grounded=True)}
    report = compose(state, SPEC)

    assert "Pleura" not in report.text
    assert "unremarkable" not in report.text.lower()
    assert set(report.omitted_fields) == {"pleura", "summary"}


def test_a_dictated_negative_is_rendered_not_dropped() -> None:
    """ "No effusion" is a statement the radiologist made. Omitting it makes the report silent on something they addressed."""
    state = _state("x")
    state.field_values = {"pleura": FieldValue(field_key="pleura", assertion_status=AssertionStatus.ABSENT, is_grounded=True)}
    report = compose(state, SPEC)
    assert "Pleura: Not present." in report.text


def test_empty_sections_are_omitted() -> None:
    state = _state("x")
    state.field_values = {"lungs": FieldValue(field_key="lungs", value_text="Clear", is_grounded=True)}
    report = compose(state, SPEC)
    assert "IMPRESSION" not in report.text


@pytest.mark.asyncio
async def test_compose_warns_when_an_auto_filled_value_reaches_it() -> None:
    """This is the stage where auto-fill would become indistinguishable from dictation in the output text."""
    state = _state("x")
    state.field_values = {"lungs": FieldValue(field_key="lungs", value_text="Unremarkable", is_grounded=True, fill_source=FillSource.TEMPLATE_DEFAULT)}
    result = await ComposeStage(SPEC).run(state, ctx=object())
    assert any("auto-filled" in w for w in result.warnings)


# =============================================== stage 15: route to human ====
def test_a_critical_alert_routes_to_a_radiologist_at_any_confidence() -> None:
    from radreport.pipeline.state import CriticalAlertState

    state = _state("x")
    state.field_values = {"a": FieldValue(field_key="a", confidence=1.0, is_grounded=True)}
    state.critical_alerts = [CriticalAlertState(rule_code="PNEUMOTHORAX", evidence_text="x", confidence=0.95)]
    outcome = decide(state)

    assert outcome.reviewer_role == ReviewerRole.RADIOLOGIST
    assert outcome.path_type == PathType.RADIOLOGIST_ONLY
    assert outcome.priority == StudyPriority.URGENT


def test_a_blocking_finding_routes_to_a_radiologist() -> None:
    state = _state("x")
    state.field_values = {"a": FieldValue(field_key="a", confidence=1.0, is_grounded=True)}
    state.verification = [VerificationFindingState(check_id="laterality_disagrees_with_source", check_type=CheckType.RULE, severity=Severity.BLOCK, message="contradiction")]
    assert decide(state).reviewer_role == ReviewerRole.RADIOLOGIST


def test_a_confident_clean_draft_goes_to_the_assistant_path() -> None:
    state = _state("x")
    state.field_values = {"a": FieldValue(field_key="a", confidence=0.95, is_grounded=True)}
    outcome = decide(state)

    assert outcome.reviewer_role == ReviewerRole.TRANSCRIPTIONIST
    assert outcome.path_type == PathType.TRANSCRIPTIONIST_REVIEWED
    assert outcome.priority == StudyPriority.ROUTINE


def test_the_grading_schedule_follows_5_4_1() -> None:
    assert grading_rate(0) == 1
    assert grading_rate(3) == 1
    assert grading_rate(5) == 5
    assert grading_rate(9) == 10
    assert grading_rate(52) == 20


def test_grading_selection_is_deterministic_across_replays() -> None:
    """An audit that disagrees with the original run about whether a report was sampled undermines the sample it exists to verify."""
    rid = uuid.uuid4()
    assert is_sampled_for_grading(rid, rate=20) == is_sampled_for_grading(rid, rate=20)
    assert is_sampled_for_grading(rid, rate=1) is True

    sampled = sum(is_sampled_for_grading(uuid.uuid4(), rate=20) for _ in range(4000))
    # ~5% of 4000 = 200. Wide bounds: this asserts the rate is roughly right,
    # not that the hash is uniform.
    assert 120 < sampled < 300
