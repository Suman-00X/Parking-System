"""The checks that must all hold before a report is filed with no reviewer, and the order they are applied in.

The refusals are the tests that matter here.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from radreport.autonomy.release import AUTONOMOUS_RELEASE_THRESHOLD, REVIEW_REDUCTION_TARGET, AutonomyGrant, Coverage, may_release_without_review
from radreport.core.types import ActorType, AutonomyStatus, CheckType, DraftStatus, PathType, ReviewerRole, Severity
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.reporting import ReportDraft
from radreport.db.models.review import FinalReport
from radreport.pipeline.stages.persist import PersistDraftStage
from radreport.pipeline.stages.providers import StaticKnowledgeProvider, TenantKnowledge
from radreport.pipeline.stages.release import AutonomousReleaseStage, ReleaseWithoutReviewError
from radreport.pipeline.stages.route_human import decide
from radreport.pipeline.state import CriticalAlertState, DictatingRadiologist, FieldValue, PipelineState, RoutingState, TranscriptState, Utterance, VerificationFindingState

GRANTED = AutonomyGrant(class_id=uuid.uuid4(), class_code="ABDO_US", status=AutonomyStatus.GRANTED)


def _ok(**overrides):
    """Every gate satisfied. Each test flips exactly one thing."""
    kwargs = {"grant": GRANTED, "confidence": 0.95, "radiologist_opted_in": True, "has_critical_alert": False, "verification_severities": frozenset(), "flagged_field_count": 0, "selected_for_grading": False}
    return may_release_without_review(**{**kwargs, **overrides})


# ====================================================== the gates, one by one ===
def test_everything_satisfied_releases() -> None:
    decision = _ok()
    assert decision.eligible
    assert decision.blocker is None
    assert decision.class_code == "ABDO_US"


def test_the_grading_sample_is_never_released() -> None:
    """The invariant revocation depends on."""
    decision = _ok(selected_for_grading=True)
    assert not decision.eligible
    assert decision.blocker == "grading_sample"


@pytest.mark.parametrize("status", [AutonomyStatus.NOT_EVALUATED, AutonomyStatus.ACCRUING, AutonomyStatus.SUSPENDED, AutonomyStatus.REVOKED])
def test_only_a_granted_class_releases(status: str) -> None:
    """Including `suspended`: suspension exists to stop release while keeping the evidence, so it must read as "not granted" here."""
    decision = _ok(grant=AutonomyGrant(class_id=uuid.uuid4(), class_code="X", status=status))
    assert not decision.eligible
    assert decision.blocker == "class_not_granted"


def test_a_template_with_no_class_does_not_release() -> None:
    """Absence is "no evidence", never "no objection"."""
    decision = _ok(grant=None)
    assert not decision.eligible
    assert decision.blocker == "no_autonomy_class"


def test_the_radiologist_must_have_opted_in() -> None:
    """A class-level grant is evidence about a body of work, not consent from the person whose name goes on the report."""
    decision = _ok(radiologist_opted_in=False)
    assert not decision.eligible
    assert decision.blocker == "radiologist_not_opted_in"


def test_a_critical_alert_blocks_release() -> None:
    decision = _ok(has_critical_alert=True)
    assert not decision.eligible
    assert decision.blocker == "critical_alert"


@pytest.mark.parametrize("severity", [Severity.BLOCK, Severity.ERROR])
def test_a_blocking_or_error_finding_blocks_release(severity: str) -> None:
    """A deterministic check about this report outranks a statistical argument about reports like it."""
    decision = _ok(verification_severities=frozenset({severity}))
    assert not decision.eligible
    assert decision.blocker == "verification_finding"


@pytest.mark.parametrize("severity", [Severity.INFO, Severity.WARN])
def test_an_informational_finding_does_not_block_release(severity: str) -> None:
    assert _ok(verification_severities=frozenset({severity})).eligible


def test_a_flagged_field_blocks_release() -> None:
    """The one case where review is doing identifiable work: a validator disagreed with the extraction and nobody would be adjudicating it."""
    decision = _ok(flagged_field_count=1)
    assert not decision.eligible
    assert decision.blocker == "flagged_fields"


def test_the_release_threshold_is_stricter_than_the_assistant_threshold() -> None:
    """0.70 is the bar for "an assistant may review this instead of a radiologist", which still has a human reading every word."""
    assert AUTONOMOUS_RELEASE_THRESHOLD > 0.70

    assert not _ok(confidence=0.75).eligible
    assert _ok(confidence=0.75).blocker == "confidence_below_release_threshold"
    assert _ok(confidence=AUTONOMOUS_RELEASE_THRESHOLD).eligible


def test_configuration_blockers_are_reported_before_per_report_ones() -> None:
    """A lab reading "confidence too low" on a class that was never granted would be chasing the wrong problem."""
    decision = _ok(grant=None, confidence=0.1, flagged_field_count=5)
    assert decision.blocker == "no_autonomy_class"


# =========================================================== stage 15 wiring ===
def _state(*, confidence: float, opted_in: bool = True) -> PipelineState:
    state = PipelineState(tenant_id=uuid.uuid4(), recording_id=uuid.uuid4(), pipeline_run_id=uuid.uuid4())
    state.field_values = {"liver": FieldValue(field_key="liver", confidence=confidence, is_grounded=True)}
    state.radiologist = DictatingRadiologist(profile_id=uuid.uuid4(), user_id=uuid.uuid4(), autonomy_enabled=opted_in)
    return state


#: Week 52 is the steady-state 1-in-20 schedule; weeks 1–4 grade every report, so nothing can be released then.
STEADY_STATE = 52


def _unsampled_state(**kwargs) -> PipelineState:
    """A state whose recording id is not in the 1-in-20 grading sample."""
    from radreport.pipeline.stages.route_human import is_sampled_for_grading

    while True:
        state = _state(**kwargs)
        if not is_sampled_for_grading(state.recording_id, rate=20):
            return state


def test_a_granted_class_releases_a_clean_confident_draft() -> None:
    outcome = decide(_unsampled_state(confidence=0.97), autonomy=GRANTED, weeks_since_go_live=STEADY_STATE)
    assert outcome.path_type == PathType.AUTONOMOUS
    assert outcome.released_without_review is True
    assert outcome.reviewer_role is None, "a released report must name no reviewer"
    assert outcome.autonomy_class_code == "ABDO_US"


def test_without_a_grant_the_same_draft_goes_to_the_assistant() -> None:
    """The pre-Phase-6 behaviour, unchanged."""
    outcome = decide(_unsampled_state(confidence=0.97), weeks_since_go_live=STEADY_STATE)
    assert outcome.path_type == PathType.TRANSCRIPTIONIST_REVIEWED
    assert outcome.reviewer_role == ReviewerRole.TRANSCRIPTIONIST
    assert outcome.released_without_review is False
    assert outcome.release_blocker == "no_autonomy_class"


def test_autonomy_never_overrides_a_critical_finding() -> None:
    """Outranks a grant. A high-confidence draft describing a pneumothorax is the most important reason to involve a radiologist."""
    state = _unsampled_state(confidence=0.99)
    state.critical_alerts = [CriticalAlertState(rule_code="PNEUMOTHORAX", evidence_text="x", confidence=0.95)]
    outcome = decide(state, autonomy=GRANTED, weeks_since_go_live=STEADY_STATE)

    assert outcome.path_type == PathType.RADIOLOGIST_ONLY
    assert outcome.reviewer_role == ReviewerRole.RADIOLOGIST
    assert outcome.released_without_review is False


def test_autonomy_never_overrides_a_blocking_finding() -> None:
    state = _unsampled_state(confidence=0.99)
    state.verification = [VerificationFindingState(check_id="laterality_disagrees_with_source", check_type=CheckType.RULE, severity=Severity.BLOCK, message="contradiction")]
    outcome = decide(state, autonomy=GRANTED, weeks_since_go_live=STEADY_STATE)

    assert outcome.path_type == PathType.RADIOLOGIST_ONLY
    assert outcome.released_without_review is False


def test_release_is_not_reachable_from_the_radiologist_path() -> None:
    """Low confidence routes to a radiologist, and a grant must not promote a report *out* of review — only ever out of the assistant path."""
    outcome = decide(_unsampled_state(confidence=0.3), autonomy=GRANTED, weeks_since_go_live=STEADY_STATE)
    assert outcome.path_type == PathType.RADIOLOGIST_ONLY
    assert outcome.released_without_review is False


def test_nothing_is_released_during_the_first_four_weeks() -> None:
    """Grades every report in weeks 1–4, so the sample is the whole volume and the grading-sample gate holds all of it back."""
    outcome = decide(_unsampled_state(confidence=0.99), autonomy=GRANTED, weeks_since_go_live=0)
    assert outcome.released_without_review is False
    assert outcome.release_blocker == "grading_sample"


def test_an_unknown_dictating_radiologist_does_not_release() -> None:
    """`state.radiologist is None` must read as "not opted in". A run assembled without it should behave as it did before unreviewed release existed."""
    state = _unsampled_state(confidence=0.99)
    state.radiologist = None
    outcome = decide(state, autonomy=GRANTED, weeks_since_go_live=STEADY_STATE)

    assert outcome.released_without_review is False
    assert outcome.release_blocker == "radiologist_not_opted_in"


def test_a_radiologist_who_has_not_opted_in_does_not_release() -> None:
    outcome = decide(_unsampled_state(confidence=0.99, opted_in=False), autonomy=GRANTED, weeks_since_go_live=STEADY_STATE)
    assert outcome.released_without_review is False
    assert outcome.release_blocker == "radiologist_not_opted_in"


def test_every_routed_report_carries_an_attributable_blocker() -> None:
    """The target is a share of volume, and you cannot raise a share you cannot attribute."""
    cases = [(0.3, GRANTED), (0.75, GRANTED), (0.99, None)]
    for confidence, autonomy in cases:
        outcome = decide(_unsampled_state(confidence=confidence), autonomy=autonomy, weeks_since_go_live=STEADY_STATE)
        assert outcome.released_without_review is False
        assert outcome.release_blocker is not None


# ========================================================= stage 17: release ===
def _released_state() -> PipelineState:
    """A state that stage 15 has already decided to release."""
    version_id = uuid.uuid4()
    state = _unsampled_state(confidence=0.97)
    state.study_id = uuid.uuid4()
    state.report_draft_id = uuid.uuid4()
    state.rendered_text = "FINDINGS\nLiver: normal"
    state.routing = RoutingState(chosen_template_version_id=version_id)
    state.human_routing = decide(state, autonomy=GRANTED, weeks_since_go_live=STEADY_STATE)
    assert state.human_routing.released_without_review
    return state


def _provider(state: PipelineState, grant: AutonomyGrant | None = GRANTED):
    assert state.routing is not None
    autonomy = {state.routing.chosen_template_version_id: grant} if grant is not None else {}
    return StaticKnowledgeProvider(TenantKnowledge(tenant_id=state.tenant_id, autonomy=autonomy))


async def _run(state: PipelineState, provider) -> object:
    return await AutonomousReleaseStage(provider).run(state, ctx=object())


@pytest.mark.asyncio
async def test_the_release_stage_files_a_report_with_no_revision() -> None:
    """The null migration 0006 exists for."""
    state = _released_state()
    result = await _run(state, _provider(state))

    reports = [w for w in result.pending_writes if isinstance(w, FinalReport)]
    assert len(reports) == 1
    report = reports[0]

    assert report.path_type == PathType.AUTONOMOUS
    assert report.final_revision_id is None
    assert report.autonomy_class_id == GRANTED.class_id
    assert report.report_draft_id == state.report_draft_id
    assert report.study_id == state.study_id


@pytest.mark.asyncio
async def test_a_released_report_is_signed_by_the_dictating_radiologist() -> None:
    """A clinical record needs an author, and "the system" is not one. The `path_type` is what records that they did not review it."""
    state = _released_state()
    assert state.radiologist is not None
    result = await _run(state, _provider(state))

    report = next(w for w in result.pending_writes if isinstance(w, FinalReport))
    assert report.signed_by == state.radiologist.user_id


@pytest.mark.asyncio
async def test_the_release_is_audited_as_a_system_action() -> None:
    """Nobody decided *this* report. The decision belonging to a person is the grant, and `autonomy_class_id` is the link back to it."""
    state = _released_state()
    result = await _run(state, _provider(state))

    audits = [w for w in result.pending_writes if isinstance(w, AuditLog)]
    assert len(audits) == 1
    assert audits[0].action == "report_released_without_review"
    assert audits[0].actor_id is None
    assert audits[0].actor_type == ActorType.SYSTEM
    assert audits[0].after["autonomy_class"] == "ABDO_US"


@pytest.mark.asyncio
async def test_the_release_stage_writes_nothing_for_a_reviewed_report() -> None:
    state = _unsampled_state(confidence=0.97)
    state.routing = RoutingState(chosen_template_version_id=uuid.uuid4())
    state.human_routing = decide(state, weeks_since_go_live=STEADY_STATE)
    assert not state.human_routing.released_without_review

    result = await _run(state, _provider(state, grant=None))
    assert result.pending_writes == []


@pytest.mark.asyncio
async def test_the_release_stage_refuses_rather_than_filing_a_gap() -> None:
    """Each of these absences would otherwise become a NULL in a legal record."""
    cases = {"report_draft_id": "nothing to release", "study_id": "study_id is NOT NULL", "radiologist": "author of record"}
    for attribute, expected in cases.items():
        state = _released_state()
        setattr(state, attribute, None)
        with pytest.raises(ReleaseWithoutReviewError, match=expected):
            await _run(state, _provider(state))


@pytest.mark.asyncio
async def test_the_release_stage_refuses_when_the_class_cannot_be_resolved() -> None:
    """Filing a report under the wrong authority is worse than not filing it, and the CHECK constraint would not accept it anyway."""
    state = _released_state()
    other = AutonomyGrant(class_id=uuid.uuid4(), class_code="SOMETHING_ELSE", status=AutonomyStatus.GRANTED)
    with pytest.raises(ReleaseWithoutReviewError, match="could not be resolved"):
        await _run(state, _provider(state, grant=other))


def _persistable(state: PipelineState) -> PipelineState:
    state.transcript = TranscriptState(text="liver is normal", version=1, stage="raw")
    state.utterances = [Utterance(seq=1, char_start=0, char_end=14, audio_start_ms=0, audio_end_ms=1000, text="liver is normal")]
    return state


@pytest.mark.asyncio
async def test_a_released_draft_is_born_signed() -> None:
    """Set by stage 16, not patched by 17, so both rows share one transaction."""
    state = _persistable(_released_state())
    result = await PersistDraftStage(_provider(state)).run(state, ctx=object())

    drafts = [w for w in result.pending_writes if isinstance(w, ReportDraft)]
    assert len(drafts) == 1
    assert drafts[0].status == DraftStatus.SIGNED
    # And stage 17 can find it without a session.
    assert state.report_draft_id == drafts[0].id


@pytest.mark.asyncio
async def test_a_reviewed_draft_is_still_born_generated() -> None:
    state = _persistable(_unsampled_state(confidence=0.97))
    state.routing = RoutingState(chosen_template_version_id=uuid.uuid4())
    state.human_routing = decide(state, weeks_since_go_live=STEADY_STATE)

    result = await PersistDraftStage(_provider(state, grant=None)).run(state, ctx=object())
    drafts = [w for w in result.pending_writes if isinstance(w, ReportDraft)]
    assert drafts[0].status == DraftStatus.GENERATED


# ================================================================= coverage ===
def test_coverage_measures_the_share_of_signed_volume() -> None:
    measured = Coverage(signed=100, released=45)
    assert measured.reviewed == 55
    assert measured.share == 0.45
    assert measured.meets_target is True


def test_coverage_below_the_target_says_so() -> None:
    measured = Coverage(signed=100, released=12)
    assert measured.share == 0.12
    assert measured.meets_target is False


def test_coverage_of_nothing_is_zero_not_a_division_error() -> None:
    measured = Coverage(signed=0, released=0, since=dt.datetime.now(dt.UTC))
    assert measured.share == 0.0
    assert measured.meets_target is False


def test_the_target_is_the_one_4_3_states() -> None:
    assert REVIEW_REDUCTION_TARGET == 0.40
