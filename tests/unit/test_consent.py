"""Training eligibility is always derived from the consent record, never set directly."""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace

from radreport.knowledge.consent import evaluate_eligibility

NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


def _tenant(**overrides: object) -> SimpleNamespace:
    base = {"id": uuid.uuid4(), "training_pooling_consent": True, "consent_effective_from": NOW - dt.timedelta(days=30), "consent_withdrawn_at": None}
    return SimpleNamespace(**{**base, **overrides})


def _recording(**overrides: object) -> SimpleNamespace:
    base = {"uploaded_at": NOW, "phi_scrub_completed_at": NOW, "tenant_id": uuid.uuid4(), "radiologist_id": uuid.uuid4(), "is_training_corpus_eligible": False}
    return SimpleNamespace(**{**base, **overrides})


def _radiologist(**overrides: object) -> SimpleNamespace:
    base = {"training_consent_ref": "CONSENT-2026-001", "voice_consent_ref": "VOICE-001"}
    return SimpleNamespace(**{**base, **overrides})


def test_all_four_conditions_met_is_eligible() -> None:
    verdict = evaluate_eligibility(_recording(), _tenant(), _radiologist())
    assert verdict.eligible
    assert verdict.reasons == ()


def test_no_tenant_pooling_clause_blocks_everything() -> None:
    """The lab-agreement clause.: free before contract #1, near-impossible to retrofit across signed labs."""
    verdict = evaluate_eligibility(_recording(), _tenant(training_pooling_consent=False), _radiologist())
    assert not verdict.eligible
    assert any("pooling consent" in r for r in verdict.reasons)


def test_audio_predating_consent_is_not_covered() -> None:
    """Granting consent does not retroactively cover what came before it."""
    verdict = evaluate_eligibility(_recording(uploaded_at=NOW - dt.timedelta(days=90)), _tenant(), _radiologist())
    assert not verdict.eligible
    assert any("predates consent" in r for r in verdict.reasons)


def test_audio_after_withdrawal_is_excluded() -> None:
    """Withdrawal stops *future* inclusion."""
    verdict = evaluate_eligibility(_recording(uploaded_at=NOW), _tenant(consent_withdrawn_at=NOW - dt.timedelta(days=1)), _radiologist())
    assert not verdict.eligible
    assert any("withdrawal" in r for r in verdict.reasons)


def test_audio_inside_a_closed_window_stays_eligible() -> None:
    """The historical window matters: audio uploaded while consent was live was lawfully included requires you to be able to say which runs used it."""
    verdict = evaluate_eligibility(_recording(uploaded_at=NOW - dt.timedelta(days=10)), _tenant(training_pooling_consent=True, consent_withdrawn_at=NOW - dt.timedelta(days=1)), _radiologist())
    assert verdict.eligible


def test_enrollment_consent_does_not_imply_training_consent() -> None:
    """Two purposes, two consents."""
    verdict = evaluate_eligibility(_recording(), _tenant(), _radiologist(training_consent_ref=None))
    assert not verdict.eligible
    assert any("training_consent_ref" in r for r in verdict.reasons)


def test_declining_training_still_allows_enrollment() -> None:
    """A radiologist who declines is still enrolled; their audio is simply never training-eligible."""
    radiologist = _radiologist(training_consent_ref=None)
    assert radiologist.voice_consent_ref
    assert not evaluate_eligibility(_recording(), _tenant(), radiologist).eligible


def test_unscrubbed_audio_is_never_eligible() -> None:
    """The problem the design doc does not raise."""
    verdict = evaluate_eligibility(_recording(phi_scrub_completed_at=None), _tenant(), _radiologist())
    assert not verdict.eligible
    assert any("PHI scrub" in r for r in verdict.reasons)


def test_every_failure_reason_is_reported_not_just_the_first() -> None:
    """An auditable answer, not a bare boolean — you have to be able to say *why* a recording was excluded, years later."""
    verdict = evaluate_eligibility(_recording(phi_scrub_completed_at=None), _tenant(training_pooling_consent=False), _radiologist(training_consent_ref=None))
    assert len(verdict.reasons) >= 3


def test_missing_radiologist_is_not_eligible() -> None:
    assert not evaluate_eligibility(_recording(), _tenant(), None).eligible
