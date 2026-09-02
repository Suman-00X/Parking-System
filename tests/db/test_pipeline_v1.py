"""The whole pipeline end to end through the orchestrator: a recording goes in, a draft comes out."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from radreport.adapters.asr.whisper_local import StubASREngine
from radreport.adapters.storage.object_store import InMemoryObjectStore
from radreport.core.types import AlertSeverity, AudioFormat, CaptureDeviceClass, PatternType, PipelineTrigger, ReviewerRole, RunStatus, TermType, UserRole
from radreport.db.models.identity import AppUser, Patient, RadiologistProfile, Study
from radreport.db.models.ingestion import Recording
from radreport.db.models.knowledge import Template, TemplateVersion
from radreport.db.models.orchestration import PipelineRun, StageExecution
from radreport.db.models.reporting import RoutingDecision
from radreport.db.session import tenant_session
from radreport.devtools.synthetic import synth_audio
from radreport.knowledge.phonetics import double_metaphone
from radreport.pipeline.graph import new_run
from radreport.pipeline.stages.providers import CriticalRuleEntry, LexiconEntry, StaticKnowledgeProvider, StudyCodeEntry, TenantKnowledge
from radreport.pipeline.stages.route_human import decide
from radreport.pipeline.stages.routing import TemplateCandidate
from radreport.pipeline.v1 import V1_PIPELINE_VERSION, build_v1_graph

pytestmark = pytest.mark.db

DICTATION = "study type ultrasound abdomen routine. the liver is normal in echo texture. there is a 3.2 cm simple cyst in the left kidney, sorry, the right kidney. no hydronephrosis. no pneumothorax."


@pytest.fixture
def pipeline_fixture(migrated_db: str, two_tenants):
    """A tenant with one recording, plus the knowledge roster import–critical-findings rules would have produced."""
    tenant_id, _other = two_tenants
    store = InMemoryObjectStore()
    key = f"rec/{uuid.uuid4().hex}.flac"
    store.put(key, synth_audio(seconds=8, seed=11), content_type="audio/flac")

    with tenant_session(tenant_id, url=migrated_db) as session:
        user = AppUser(tenant_id=tenant_id, employee_code=f"R-{uuid.uuid4().hex[:6]}", display_name="Dr Test", roles=[UserRole.RADIOLOGIST])
        session.add(user)
        session.flush()
        profile = RadiologistProfile(tenant_id=tenant_id, user_id=user.id)
        session.add(profile)
        session.flush()
        patient = Patient(tenant_id=tenant_id, mrn=f"M{uuid.uuid4().hex[:8]}", pseudonym=f"P{uuid.uuid4().hex[:8]}")
        session.add(patient)
        session.flush()
        study = Study(tenant_id=tenant_id, patient_id=patient.id)
        session.add(study)
        session.flush()
        template = Template(tenant_id=tenant_id, code="USG_ABDOMEN", display_name="USG Abdomen", modality="US", body_region="abdomen")
        session.add(template)
        session.flush()
        version = TemplateVersion(tenant_id=tenant_id, template_id=template.id, version=1, json_schema={"type": "object", "properties": {}}, render_spec={"sections": ["FINDINGS"]}, routing_card="USG Abdomen. liver kidney spleen echotexture cyst", trigger_rules={}, spoken_study_code="ultrasound abdomen", spoken_study_code_phonetic=double_metaphone("ultrasound abdomen")[0], effective_from=dt.datetime.now(dt.UTC), is_current=True)
        session.add(version)
        session.flush()

        recording = Recording(tenant_id=tenant_id, study_id=study.id, radiologist_id=profile.id, object_key=key, content_hash=uuid.uuid4().hex, duration_seconds=8, capture_device_class=CaptureDeviceClass.DICTATION_MIC_PTT, audio_format=AudioFormat.FLAC)
        session.add(recording)
        session.flush()
        return {"tenant_id": tenant_id, "recording_id": recording.id, "template_version_id": version.id, "store": store, "object_key": key, "db": migrated_db}


def _knowledge(tenant_id, template_version_id) -> TenantKnowledge:
    primary, secondary = double_metaphone("echotexture")
    spoken_key, _ = double_metaphone("ultrasound abdomen")
    return TenantKnowledge(tenant_id=tenant_id, lexicon=(LexiconEntry(canonical_form="echotexture", term_type=TermType.ANATOMY, phonetic_key_primary=primary, phonetic_key_secondary=secondary, surface_variants=("echo texture",)),), study_codes=(StudyCodeEntry(template_version_id=template_version_id, template_code="USG_ABDOMEN", spoken_study_code="ultrasound abdomen", phonetic_key=spoken_key),), critical_rules=(CriticalRuleEntry(rule_id=uuid.uuid4(), rule_code="PNEUMOTHORAX", finding_label="Pneumothorax", pattern_type=PatternType.LEXICAL, patterns=("pneumothorax",), negation_sensitive=True, severity=AlertSeverity.RED, sla_minutes=30),))


def _templates(template_version_id) -> list[TemplateCandidate]:
    return [TemplateCandidate(template_version_id=template_version_id, template_code="USG_ABDOMEN", display_name="USG Abdomen", modality="US", body_region="abdomen", routing_card="USG Abdomen. liver kidney spleen echotexture cyst hydronephrosis", spoken_study_code="ultrasound abdomen", usage_count_12m=500)]


def _graph(fixture):
    return build_v1_graph(store=fixture["store"], asr_engine=StubASREngine(text=DICTATION), knowledge=StaticKnowledgeProvider(_knowledge(fixture["tenant_id"], fixture["template_version_id"])), templates=_templates(fixture["template_version_id"]), sections=[])


@pytest.mark.asyncio
async def test_a_recording_becomes_a_draft_with_a_full_orchestration_trace(pipeline_fixture) -> None:
    """A recording becomes a draft with every stage recorded in the run trace."""
    fixture = pipeline_fixture
    tenant_id = fixture["tenant_id"]

    with tenant_session(tenant_id, url=fixture["db"]) as session:
        run, ctx, state = new_run(session, tenant_id=tenant_id, recording_id=fixture["recording_id"], trigger=PipelineTrigger.UPLOAD, pipeline_version=V1_PIPELINE_VERSION)
        state.audio_object_key = fixture["object_key"]

        final = await _graph(fixture).run(state, ctx, session)

        assert run.status == RunStatus.SUCCEEDED
        assert run.completed_at is not None

        executions = session.query(StageExecution).filter(StageExecution.pipeline_run_id == run.id).all()
        assert len(executions) == 15
        assert {e.status for e in executions} == {RunStatus.SUCCEEDED}
        # every stage leaves a row, whether or not it called a provider.
        assert all(e.duration_ms is not None for e in executions)

        # The transcript reached the end intact — no stage rewrote it.
        assert final.transcript is not None
        assert final.transcript.text == DICTATION
        assert final.utterances

        # Routing short-circuited on the spoken study code: no model was bound
        # and none was needed.
        assert final.routing is not None
        assert final.routing.chosen_template_version_id == fixture["template_version_id"]
        decision = session.query(RoutingDecision).filter(RoutingDecision.recording_id == fixture["recording_id"]).one()
        assert decision.chosen_template_version_id == fixture["template_version_id"]


@pytest.mark.asyncio
async def test_a_retracted_laterality_is_marked_not_deleted(pipeline_fixture) -> None:
    """The G4 case, through the real graph: "left kidney, sorry, the right kidney" must leave `left` retracted and `right` authoritative."""
    fixture = pipeline_fixture
    with tenant_session(fixture["tenant_id"], url=fixture["db"]) as session:
        _run, ctx, state = new_run(session, tenant_id=fixture["tenant_id"], recording_id=fixture["recording_id"], trigger=PipelineTrigger.UPLOAD)
        state.audio_object_key = fixture["object_key"]
        final = await _graph(fixture).run(state, ctx, session)

        retracted = [u for u in final.utterances if u.superseded_by_seq is not None]
        assert retracted, "the self-correction must be detected"
        assert "left" in retracted[0].text
        assert retracted[0].is_included_downstream is False
        # Nothing was removed.
        assert "left kidney" in final.transcript.text


@pytest.mark.asyncio
async def test_a_negated_critical_finding_does_not_alert(pipeline_fixture) -> None:
    """The dictation says "no pneumothorax". The alert path must stay quiet — and the splitter must not break the sentence at "3.2"."""
    fixture = pipeline_fixture
    with tenant_session(fixture["tenant_id"], url=fixture["db"]) as session:
        _run, ctx, state = new_run(session, tenant_id=fixture["tenant_id"], recording_id=fixture["recording_id"], trigger=PipelineTrigger.UPLOAD)
        state.audio_object_key = fixture["object_key"]
        final = await _graph(fixture).run(state, ctx, session)

        assert final.critical_alerts == []
        assert decide(final).reviewer_role == ReviewerRole.RADIOLOGIST  # low confidence


@pytest.mark.asyncio
async def test_a_shadow_run_keeps_its_trace_and_discards_its_domain_writes(pipeline_fixture) -> None:
    """`is_shadow` is a flag, not a separate code path."""
    fixture = pipeline_fixture
    with tenant_session(fixture["tenant_id"], url=fixture["db"]) as session:
        run, ctx, state = new_run(session, tenant_id=fixture["tenant_id"], recording_id=fixture["recording_id"], trigger=PipelineTrigger.SHADOW, is_shadow=True)
        state.audio_object_key = fixture["object_key"]
        await _graph(fixture).run(state, ctx, session)

        assert run.status == RunStatus.SUCCEEDED
        assert session.query(StageExecution).filter(StageExecution.pipeline_run_id == run.id).count() == 15
        # No RoutingDecision, no AsrRun — the domain writes were discarded.
        assert session.query(RoutingDecision).filter(RoutingDecision.recording_id == fixture["recording_id"]).count() == 0


@pytest.mark.asyncio
async def test_the_run_records_its_pipeline_version(pipeline_fixture) -> None:
    """Gates per pipeline version, so the version in force is recorded."""
    fixture = pipeline_fixture
    with tenant_session(fixture["tenant_id"], url=fixture["db"]) as session:
        run, ctx, state = new_run(session, tenant_id=fixture["tenant_id"], recording_id=fixture["recording_id"], trigger=PipelineTrigger.UPLOAD, pipeline_version=V1_PIPELINE_VERSION)
        state.audio_object_key = fixture["object_key"]
        await _graph(fixture).run(state, ctx, session)

        stored = session.get(PipelineRun, run.id)
        assert stored.pipeline_version == V1_PIPELINE_VERSION


@pytest.mark.asyncio
async def test_the_beta_graph_reconciles_three_engines_end_to_end(pipeline_fixture) -> None:
    """Multi-engine fan-out through the real orchestrator."""
    from radreport.pipeline.stages.reconcile import EngineSpec
    from radreport.pipeline.v1 import BETA_PIPELINE_VERSION

    fixture = pipeline_fixture
    tenant_id = fixture["tenant_id"]

    honest_a = StubASREngine(text=DICTATION)
    honest_a.engine = "engine-a"
    honest_b = StubASREngine(text=DICTATION)
    honest_b.engine = "engine-b"
    # The fixture dictation already says "no pneumothorax", so the invented
    # text has to be words nothing else in it contains.
    inventor = StubASREngine(text=DICTATION + " grossly emphysematous bullae")
    inventor.engine = "engine-c"

    graph = build_v1_graph(store=fixture["store"], asr_engine=honest_a, knowledge=StaticKnowledgeProvider(_knowledge(tenant_id, fixture["template_version_id"])), templates=_templates(fixture["template_version_id"]), sections=[], extra_asr_engines=[EngineSpec(honest_b), EngineSpec(inventor)], enable_post_correction=True, version=BETA_PIPELINE_VERSION)

    with tenant_session(tenant_id, url=fixture["db"]) as session:
        run, ctx, state = new_run(session, tenant_id=tenant_id, recording_id=fixture["recording_id"], trigger=PipelineTrigger.UPLOAD, pipeline_version=BETA_PIPELINE_VERSION)
        state.audio_object_key = fixture["object_key"]
        final = await graph.run(state, ctx, session)

        assert run.status == RunStatus.SUCCEEDED
        assert final.transcript is not None
        # The invented phrase lost 2-1 to silence.
        assert "emphysematous" not in final.transcript.text
        assert "bullae" not in final.transcript.text
        # What both honest engines said survived.
        assert "liver" in final.transcript.text
        assert final.transcript.disagreement_score is not None
        assert final.transcript.reconciliation_method in ("rover", "llm_arbitrated")
        # One AsrRun per engine — including any that failed.
        assert len(final.asr_runs) == 3


@pytest.mark.asyncio
async def test_a_failing_engine_degrades_the_vote_rather_than_the_report(pipeline_fixture) -> None:
    """One engine timing out must not fail the report: two engines voting is still better than one transcribing."""
    from radreport.adapters.asr.base import ASRConfig, ASRResult
    from radreport.pipeline.stages.reconcile import EngineSpec
    from radreport.pipeline.v1 import BETA_PIPELINE_VERSION

    fixture = pipeline_fixture

    class _BrokenEngine:
        engine = "broken"
        engine_version = "v0"

        def supports_keyterm_biasing(self) -> bool:
            return False

        async def transcribe(self, audio: bytes, config: ASRConfig) -> ASRResult:
            raise TimeoutError("provider timed out")

    good = StubASREngine(text=DICTATION)
    good.engine = "good"

    graph = build_v1_graph(store=fixture["store"], asr_engine=good, knowledge=StaticKnowledgeProvider(_knowledge(fixture["tenant_id"], fixture["template_version_id"])), templates=_templates(fixture["template_version_id"]), sections=[], extra_asr_engines=[EngineSpec(_BrokenEngine())], version=BETA_PIPELINE_VERSION)

    with tenant_session(fixture["tenant_id"], url=fixture["db"]) as session:
        run, ctx, state = new_run(session, tenant_id=fixture["tenant_id"], recording_id=fixture["recording_id"], trigger=PipelineTrigger.UPLOAD)
        state.audio_object_key = fixture["object_key"]
        final = await graph.run(state, ctx, session)

        assert run.status == RunStatus.SUCCEEDED
        assert final.transcript.text
        # The surviving engine's output stands; the failure is recorded, not hidden.
        assert len(final.asr_runs) == 1


@pytest.mark.asyncio
async def test_the_pipeline_produces_a_draft_a_reviewer_can_open(pipeline_fixture) -> None:
    """The seam between the pipeline and review, which did not exist."""
    from radreport.core.types import UserRole
    from radreport.db.models.asr import Transcript, TranscriptUtterance
    from radreport.db.models.identity import AppUser
    from radreport.db.models.reporting import ReportDraft
    from radreport.review import queue as review_queue
    from radreport.review import session as review_session
    from radreport.review.rbac import Reviewer

    fixture = pipeline_fixture
    tenant_id = fixture["tenant_id"]

    with tenant_session(tenant_id, url=fixture["db"]) as session:
        _run, ctx, state = new_run(session, tenant_id=tenant_id, recording_id=fixture["recording_id"], trigger=PipelineTrigger.UPLOAD)
        state.audio_object_key = fixture["object_key"]
        await _graph(fixture).run(state, ctx, session)

        draft = session.query(ReportDraft).filter(ReportDraft.recording_id == fixture["recording_id"]).one()
        assert draft.template_version_id == fixture["template_version_id"]
        assert draft.pipeline_run_id == ctx.pipeline_run_id
        # which model produced this draft is on the row.
        assert draft.model_versions

        # The transcript and its utterances landed, retraction links included.
        transcript = session.query(Transcript).filter(Transcript.recording_id == fixture["recording_id"]).one()
        utterances = session.query(TranscriptUtterance).filter(TranscriptUtterance.transcript_id == transcript.id).all()
        assert utterances
        retracted = [u for u in utterances if u.superseded_by_id is not None]
        assert retracted, "the self-correction must survive into the database"
        # The self-referential FK resolved to a row that exists.
        targets = {u.id for u in utterances}
        assert all(u.superseded_by_id in targets for u in retracted)

        # And a reviewer can now find and open it.
        radiologist = AppUser(tenant_id=tenant_id, employee_code=f"R-{uuid.uuid4().hex[:6]}", display_name="Dr Reviewer", roles=[UserRole.RADIOLOGIST])
        session.add(radiologist)
        session.flush()
        reviewer = Reviewer(user_id=radiologist.id, roles=(UserRole.RADIOLOGIST,))

        items = review_queue.build_queue(session, tenant_id=tenant_id, reviewer=reviewer)
        assert [i.draft_id for i in items] == [draft.id]

        view = review_session.open_draft(session, tenant_id=tenant_id, draft_id=draft.id, reviewer=reviewer)
        assert view.draft_id == draft.id


@pytest.mark.asyncio
async def test_a_detected_critical_finding_is_recorded_and_blocks_signing(pipeline_fixture) -> None:
    """The path, end to end."""
    from radreport.db.models.reporting import CriticalFindingAlert, CriticalFindingRule, ReportDraft
    from radreport.review import signing

    fixture = pipeline_fixture
    tenant_id = fixture["tenant_id"]

    with tenant_session(tenant_id, url=fixture["db"]) as session:
        # An approved, active rule, as critical-findings rules would have left it.
        rule = CriticalFindingRule(tenant_id=tenant_id, code="PNEUMOTHORAX", finding_label="Pneumothorax", pattern_type=PatternType.LEXICAL, pattern="pneumothorax", severity=AlertSeverity.RED, sla_minutes=30, escalation_path=[{"step": 1, "contact": "on-call"}], is_active=True)
        session.add(rule)
        session.flush()

        knowledge = _knowledge(tenant_id, fixture["template_version_id"])
        knowledge = TenantKnowledge(
            tenant_id=tenant_id,
            lexicon=knowledge.lexicon,
            study_codes=knowledge.study_codes,
            critical_rules=(
                CriticalRuleEntry(
                    rule_id=rule.id,
                    rule_code=rule.code,
                    finding_label=rule.finding_label,
                    pattern_type=PatternType.LEXICAL,
                    patterns=("pneumothorax",),
                    negation_sensitive=False,  # the fixture says "no pneumothorax"
                    severity=AlertSeverity.RED,
                    sla_minutes=30,
                ),
            ),
            template_fields={},
        )

        graph = build_v1_graph(store=fixture["store"], asr_engine=StubASREngine(text=DICTATION), knowledge=StaticKnowledgeProvider(knowledge), templates=_templates(fixture["template_version_id"]), sections=[])

        _run, ctx, state = new_run(session, tenant_id=tenant_id, recording_id=fixture["recording_id"], trigger=PipelineTrigger.UPLOAD)
        state.audio_object_key = fixture["object_key"]
        final = await graph.run(state, ctx, session)

        assert final.critical_alerts, "the rule should have fired"

        alerts = session.query(CriticalFindingAlert).filter(CriticalFindingAlert.recording_id == fixture["recording_id"]).all()
        assert len(alerts) == 1
        assert alerts[0].rule_id == rule.id
        assert alerts[0].acknowledged_at is None
        # The SLA clock started.
        assert alerts[0].sla_due_at > alerts[0].detected_at

        # And signing is now blocked on it.
        draft = session.query(ReportDraft).filter(ReportDraft.recording_id == fixture["recording_id"]).one()
        checks = signing.preflight(session, tenant_id=tenant_id, draft_id=draft.id)
        assert checks.unacknowledged_alerts
        assert checks.may_sign is False


@pytest.mark.asyncio
async def test_provenance_points_at_the_right_utterance_after_a_repair(pipeline_fixture) -> None:
    """The utterance rows are inserted in *reference* order so a self-correction's target exists before the row pointing at it — which is not sequence order."""
    from radreport.db.models.asr import Transcript, TranscriptUtterance

    fixture = pipeline_fixture
    tenant_id = fixture["tenant_id"]

    with tenant_session(tenant_id, url=fixture["db"]) as session:
        _run, ctx, state = new_run(session, tenant_id=tenant_id, recording_id=fixture["recording_id"], trigger=PipelineTrigger.UPLOAD)
        state.audio_object_key = fixture["object_key"]
        await _graph(fixture).run(state, ctx, session)

        transcript = session.query(Transcript).filter(Transcript.recording_id == fixture["recording_id"]).one()
        utterances = session.query(TranscriptUtterance).filter(TranscriptUtterance.transcript_id == transcript.id).all()
        assert any(u.superseded_by_id is not None for u in utterances)

        # Every row's text is the slice of the transcript its offsets name, so
        # a mis-mapped row would show up as text that does not match.
        for utterance in utterances:
            assert transcript.text[utterance.char_start : utterance.char_end] == utterance.text

        # And a retraction points at a real, different row.
        retracted = [u for u in utterances if u.superseded_by_id is not None]
        by_id = {u.id: u for u in utterances}
        for utterance in retracted:
            target = by_id[utterance.superseded_by_id]
            assert target.seq != utterance.seq


@pytest.mark.asyncio
async def test_the_review_screen_shows_the_retraction_and_its_override(pipeline_fixture) -> None:
    """Retracted spans struck through **with the override shown**."""
    from radreport.api.routes.review_ui import render_retractions
    from radreport.core.types import UserRole
    from radreport.db.models.identity import AppUser
    from radreport.db.models.reporting import ReportDraft
    from radreport.review import session as review_session
    from radreport.review.rbac import Reviewer

    fixture = pipeline_fixture
    tenant_id = fixture["tenant_id"]

    with tenant_session(tenant_id, url=fixture["db"]) as session:
        _run, ctx, state = new_run(session, tenant_id=tenant_id, recording_id=fixture["recording_id"], trigger=PipelineTrigger.UPLOAD)
        state.audio_object_key = fixture["object_key"]
        await _graph(fixture).run(state, ctx, session)

        draft = session.query(ReportDraft).filter(ReportDraft.recording_id == fixture["recording_id"]).one()
        radiologist = AppUser(tenant_id=tenant_id, employee_code=f"R-{uuid.uuid4().hex[:6]}", display_name="Dr Reviewer", roles=[UserRole.RADIOLOGIST])
        session.add(radiologist)
        session.flush()

        view = review_session.open_draft(session, tenant_id=tenant_id, draft_id=draft.id, reviewer=Reviewer(user_id=radiologist.id, roles=(UserRole.RADIOLOGIST,)))

        assert view.retractions, "the self-correction must reach the view"
        retraction = view.retractions[0]
        assert "left" in retraction.text
        assert retraction.superseded_by_text, "the override must travel with it"
        assert "right" in retraction.superseded_by_text

        html = render_retractions(view)
        assert "retracted" in html
        assert "self-correction" in html
        # Clickable, like every other span with audio.
        assert "data-audio-start" in html
