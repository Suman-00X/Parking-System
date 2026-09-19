"""Assembling the held-back gold set and running a live engine comparison over it."""

from __future__ import annotations

import uuid

import pytest

from radreport.adapters.asr.whisper_local import StubASREngine
from radreport.adapters.storage.object_store import InMemoryObjectStore
from radreport.core.errors import EvalSetLeakage
from radreport.core.types import AudioFormat, AudioQualityBucket, CaptureDeviceClass, UserRole, VerbatimSource
from radreport.db.models.adaptation import VerbatimTranscript
from radreport.db.models.evaluation import EvalItem, EvalSet
from radreport.db.models.identity import AppUser, Patient, RadiologistProfile, Study
from radreport.db.models.ingestion import Recording
from radreport.db.session import tenant_session
from radreport.devtools.synthetic import synth_audio, synth_patient_fields
from radreport.eval import goldset
from radreport.eval.bakeoff import run_bakeoff

pytestmark = pytest.mark.db

REFERENCE = "ct chest plain the lungs are normal in attenuation no pleural effusion"


@pytest.fixture
def corpus_of_recordings(migrated_db: str, two_tenants):
    """Three speakers × several recordings, spanning both capture partitions."""
    tenant_id, _other = two_tenants
    store = InMemoryObjectStore()

    with tenant_session(tenant_id, url=migrated_db) as session:
        annotator = AppUser(tenant_id=tenant_id, employee_code=f"T-{uuid.uuid4().hex[:6]}", display_name="Asha Rao", roles=[UserRole.TRANSCRIPTIONIST])
        session.add(annotator)
        session.flush()

        profiles = []
        for i in range(3):
            user = AppUser(tenant_id=tenant_id, employee_code=f"R-{uuid.uuid4().hex[:6]}", display_name=f"Dr {i}", roles=[UserRole.RADIOLOGIST])
            session.add(user)
            session.flush()
            profile = RadiologistProfile(tenant_id=tenant_id, user_id=user.id)
            session.add(profile)
            session.flush()
            profiles.append(profile)

        made: list[dict] = []
        for index in range(12):
            profile = profiles[index % 3]
            device_class = CaptureDeviceClass.LEGACY if index % 4 == 3 else CaptureDeviceClass.DICTATION_MIC_PTT
            patient = Patient(tenant_id=tenant_id, **synth_patient_fields(seed=index))
            session.add(patient)
            session.flush()
            study = Study(tenant_id=tenant_id, patient_id=patient.id)
            session.add(study)
            session.flush()

            audio = synth_audio(seconds=5, seed=index)
            key = f"rec/{uuid.uuid4().hex}.flac"
            store.put(key, audio, content_type="audio/flac")

            recording = Recording(tenant_id=tenant_id, study_id=study.id, radiologist_id=profile.id, object_key=key, content_hash=uuid.uuid4().hex, duration_seconds=300, measured_snr_db=25.0 if index % 3 == 0 else 14.0, capture_device_class=device_class, audio_format=AudioFormat.FLAC)
            session.add(recording)
            session.flush()

            session.add(VerbatimTranscript(tenant_id=tenant_id, recording_id=recording.id, text=REFERENCE, source=VerbatimSource.HUMAN_ANNOTATION, annotator_id=annotator.id, includes_disfluencies=True, audio_duration_seconds=300, capture_device_class=device_class))
            made.append({"recording_id": recording.id, "device_class": device_class})
        session.flush()
        return {"tenant_id": tenant_id, "store": store, "recordings": made}


def _new_set(session, tenant_id, *, canonical: bool) -> EvalSet:
    eval_set = EvalSet(tenant_id=None if canonical else tenant_id, name=f"{'canonical' if canonical else 'acceptance'}-{uuid.uuid4().hex[:8]}", is_canonical=canonical, is_frozen=False)
    session.add(eval_set)
    session.flush()
    return eval_set


def test_assembly_splits_the_partitions_and_spreads_across_speakers(migrated_db: str, corpus_of_recordings) -> None:
    """The split, and the speaker spread that keeps it meaningful."""
    tenant_id = corpus_of_recordings["tenant_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        eval_set = _new_set(session, tenant_id, canonical=False)
        candidates = goldset.eligible_candidates(session, tenant_id=tenant_id)
        assert len(candidates) == 12

        result = goldset.assemble(session, eval_set=eval_set, candidates=candidates, target_current=6, target_legacy=3)
        counts = result.by_device_class
        assert counts[CaptureDeviceClass.DICTATION_MIC_PTT] == 6
        assert counts[CaptureDeviceClass.LEGACY] == 3

        summary = goldset.partition_summary(session, eval_set_id=eval_set.id)
        assert set(summary) == {CaptureDeviceClass.DICTATION_MIC_PTT, CaptureDeviceClass.LEGACY}
        # Both quality buckets are represented, not just the clean audio.
        assert AudioQualityBucket.CLEAN in summary[CaptureDeviceClass.DICTATION_MIC_PTT]

        speakers = {r.radiologist_id for r in session.query(Recording).filter(Recording.id.in_(session.query(EvalItem.recording_id).filter(EvalItem.eval_set_id == eval_set.id))).all()}
        assert len(speakers) == 3, "all three speakers must appear in the set"


def test_membership_permanently_excludes_a_transcript_from_training(migrated_db: str, corpus_of_recordings) -> None:
    """Gold-set membership permanently bars a transcript from training, written in at assembly rather than checked later."""
    tenant_id = corpus_of_recordings["tenant_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        eval_set = _new_set(session, tenant_id, canonical=False)
        goldset.assemble(session, eval_set=eval_set, candidates=goldset.eligible_candidates(session, tenant_id=tenant_id), target_current=4, target_legacy=2)

        member_recordings = [r for r in session.query(EvalItem.recording_id).filter(EvalItem.eval_set_id == eval_set.id).all()]
        member_ids = [r[0] for r in member_recordings]
        flagged = session.query(VerbatimTranscript).filter(VerbatimTranscript.tenant_id == tenant_id, VerbatimTranscript.recording_id.in_(member_ids)).all()
        assert flagged and all(t.is_eval_set_member for t in flagged)

        # And the belt-and-braces check fires for a corpus built elsewhere.
        with pytest.raises(EvalSetLeakage):
            goldset.assert_no_training_leakage(session, eval_set_id=eval_set.id, training_recording_ids=member_ids)


def test_freezing_refuses_a_set_that_would_publish_meaningless_numbers(migrated_db: str, corpus_of_recordings) -> None:
    """Three refusals, each for a different way a set lies."""
    tenant_id = corpus_of_recordings["tenant_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        candidates = goldset.eligible_candidates(session, tenant_id=tenant_id)

        # 1. Too few items.
        short = _new_set(session, tenant_id, canonical=False)
        goldset.assemble(session, eval_set=short, candidates=candidates, target_current=2, target_legacy=1)
        with pytest.raises(ValueError, match="below the floor"):
            goldset.freeze(session, eval_set=short)

        # 2. Legacy only — answers nothing forward-looking.
        legacy_only = _new_set(session, tenant_id, canonical=False)
        goldset.assemble(session, eval_set=legacy_only, candidates=[c for c in candidates if c.capture_device_class == CaptureDeviceClass.LEGACY], target_current=0, target_legacy=3)
        with pytest.raises(ValueError, match="no `current`-partition items"):
            goldset.freeze(session, eval_set=legacy_only, min_items=1)

        # 3. An item with no verbatim reference.
        ok = _new_set(session, tenant_id, canonical=False)
        goldset.assemble(session, eval_set=ok, candidates=candidates, target_current=6, target_legacy=3)
        orphan = session.query(EvalItem).filter(EvalItem.eval_set_id == ok.id).first()
        orphan.gold_transcript_verbatim = None
        session.flush()
        with pytest.raises(ValueError, match="no verbatim reference"):
            goldset.freeze(session, eval_set=ok, min_items=1)


def test_a_frozen_set_refuses_further_assembly(migrated_db: str, corpus_of_recordings) -> None:
    """Adding an item after freezing silently changes what a published gate result meant."""
    tenant_id = corpus_of_recordings["tenant_id"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        eval_set = _new_set(session, tenant_id, canonical=False)
        candidates = goldset.eligible_candidates(session, tenant_id=tenant_id)
        goldset.assemble(session, eval_set=eval_set, candidates=candidates, target_current=6, target_legacy=3)
        goldset.freeze(session, eval_set=eval_set, min_items=1)
        assert eval_set.is_frozen is True

        with pytest.raises(ValueError, match="frozen"):
            goldset.assemble(session, eval_set=eval_set, candidates=candidates, target_current=1)


@pytest.mark.asyncio
async def test_a_bakeoff_scores_engines_against_a_frozen_set(migrated_db: str, corpus_of_recordings) -> None:
    """Two stub engines, one accurate and one that invents a word."""
    tenant_id = corpus_of_recordings["tenant_id"]
    store = corpus_of_recordings["store"]

    with tenant_session(tenant_id, url=migrated_db) as session:
        eval_set = _new_set(session, tenant_id, canonical=False)
        goldset.assemble(session, eval_set=eval_set, candidates=goldset.eligible_candidates(session, tenant_id=tenant_id), target_current=6, target_legacy=3)

        unfrozen = eval_set
        with pytest.raises(ValueError, match="not frozen"):
            await run_bakeoff(session, eval_set=unfrozen, engines=[StubASREngine(text=REFERENCE)], store=store)

        goldset.freeze(session, eval_set=eval_set, min_items=1)

        accurate = StubASREngine(text=REFERENCE)
        accurate.engine = "accurate"
        # Same words, plus three invented ones: errors are all insertions.
        inventor = StubASREngine(text=REFERENCE + " large left pneumothorax")
        inventor.engine = "inventor"

        report = await run_bakeoff(session, eval_set=eval_set, engines=[accurate, inventor], store=store)

        assert len(report.engines) == 2
        by_name = {r.engine: r for r in report.engines}
        assert by_name["accurate"].current.wer == 0.0
        assert by_name["inventor"].current.wer > 0.0
        assert by_name["inventor"].current.insertion_share_of_errors == 1.0
        assert by_name["inventor"].current.invents_text is True

        # Both partitions measured, kept separate.
        assert set(by_name["accurate"].partitions) == {"current", "legacy"}
        assert by_name["accurate"].current.item_count == 6
        assert by_name["accurate"].partitions["legacy"].item_count == 3

        assert report.recommend().engine == "accurate"
        assert [r.engine for r in report.rank()] == ["accurate", "inventor"]
