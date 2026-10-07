"""Operational settings: lab over platform over environment over default, audited, isolated per lab, and read by the gates."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from radreport.adaptation.gates import gate_g3_speaker_balance
from radreport.core import system_config
from radreport.core.types import AdaptationTarget, PlatformRole
from radreport.db.models.orchestration import AuditLog
from radreport.db.session import system_session, tenant_session
from tests.db.helpers import make_platform_user, signed_in

pytestmark = pytest.mark.db

KEY = "adapter.min_speakers"


@pytest.fixture(autouse=True)
def _clean_platform_rows(migrated_db: str):
    yield
    with system_session(migrated_db) as session:
        for name in system_config.SETTINGS:
            system_config.reset_value(session, name, actor_id=uuid.uuid4())


def test_the_default_applies_when_nothing_is_set(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIN_SPEAKERS_FOR_ADAPTER", raising=False)
    with system_session(migrated_db) as session:
        resolved = system_config.resolve(session, KEY)
    assert (resolved.value, resolved.source) == (5, "default")


def test_an_environment_variable_overrides_the_default(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIN_SPEAKERS_FOR_ADAPTER", "3")
    monkeypatch.setenv("ADAPTER_HOURS_THRESHOLD", "15.0")
    with system_session(migrated_db) as session:
        thresholds = system_config.adapter_thresholds(session)
    assert thresholds.min_speakers == 3 and thresholds.global_hours == 15.0


def test_an_out_of_range_environment_value_is_ignored(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_SPEAKER_SHARE", "7")
    with system_session(migrated_db) as session:
        assert system_config.resolve(session, "adapter.max_speaker_share").source == "default"


def test_a_lab_value_beats_the_platform_value_and_stays_in_its_lab(migrated_db: str, two_tenants, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIN_SPEAKERS_FOR_ADAPTER", "9")
    lab_a, lab_b = two_tenants
    actor = uuid.uuid4()
    with system_session(migrated_db) as session:
        assert system_config.set_value(session, KEY, 4, actor_id=actor).source == "platform"
    with tenant_session(lab_a, url=migrated_db) as session:
        system_config.set_value(session, KEY, 2, actor_id=actor, tenant_id=lab_a)
    with tenant_session(lab_a, url=migrated_db) as session:
        resolved = system_config.resolve(session, KEY, tenant_id=lab_a)
        assert (resolved.value, resolved.source, resolved.platform_value) == (2, "lab", 4)
    with tenant_session(lab_b, url=migrated_db) as session:
        # Row-level security hides lab A's override from lab B's session entirely.
        assert system_config.resolve(session, KEY, tenant_id=lab_b).value == 4
        assert system_config.resolve(session, KEY, tenant_id=lab_a).value == 4
    # The platform change is audited platform-wide, the lab's change inside the lab, where only that lab can read it.
    with system_session(migrated_db) as session:
        assert session.execute(select(AuditLog.action).where(AuditLog.actor_id == actor)).scalars().all() == ["system_config_set"]
    with tenant_session(lab_a, url=migrated_db) as session:
        assert "system_config_set" in session.execute(select(AuditLog.action).where(AuditLog.actor_id == actor, AuditLog.tenant_id == lab_a)).scalars().all()


def test_bad_values_are_refused(migrated_db: str) -> None:
    with system_session(migrated_db) as session:
        with pytest.raises(system_config.SettingRefused):
            system_config.set_value(session, KEY, 0, actor_id=uuid.uuid4())
        with pytest.raises(system_config.SettingRefused):
            system_config.set_value(session, KEY, 2.5, actor_id=uuid.uuid4())
        with pytest.raises(system_config.SettingRefused):
            system_config.set_value(session, "adapter.nonsense", 1, actor_id=uuid.uuid4())


def test_the_speaker_gate_uses_the_configured_minimum() -> None:
    from radreport.adaptation.gates import _CorpusItem

    corpus = [_CorpusItem(recording_id=uuid.uuid4(), radiologist_id=uuid.uuid4(), hours=1.0, capture_device_class="dictation_mic_ptt", includes_disfluencies=True, is_eval_set_member=False) for _ in range(3)]
    assert not gate_g3_speaker_balance(corpus, target=AdaptationTarget.ASR_GLOBAL).passed
    relaxed = system_config.AdapterThresholds(min_speakers=3, max_speaker_share=0.5)
    assert gate_g3_speaker_balance(corpus, target=AdaptationTarget.ASR_GLOBAL, thresholds=relaxed).passed


def test_the_settings_page_and_api(migrated_db: str, two_tenants) -> None:
    lab, _ = two_tenants
    admin = signed_in(make_platform_user(migrated_db))
    assert "Speech adaptation gates" in admin.get("/admin/config").text
    saved = admin.post(f"/admin/config/{KEY}", data={"value": "6"})
    assert saved.status_code == 303 and "error" not in saved.headers["location"]
    api = {s["key"]: s for s in admin.get("/admin/api/ops/config").json()}
    assert (api[KEY]["value"], api[KEY]["source"]) == (6, "platform")
    lab_set = admin.post(f"/admin/api/ops/config/{KEY}", json={"value": 3, "tenant_id": str(lab)})
    assert lab_set.status_code == 200 and lab_set.json()["source"] == "lab"
    assert admin.get(f"/admin/config?lab={lab}").status_code == 200
    assert admin.post(f"/admin/api/ops/config/{KEY}/reset", json={"tenant_id": str(lab)}).json()["source"] == "platform"
    assert admin.post(f"/admin/api/ops/config/{KEY}", json={"value": 0}).status_code == 422

    support = signed_in(make_platform_user(migrated_db, role=PlatformRole.SUPPORT))
    assert support.get("/admin/config").status_code == 200
    assert support.post(f"/admin/config/{KEY}", data={"value": "6"}).status_code == 403
