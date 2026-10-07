"""Operational thresholds that ops can change without a release: a typed registry and how a value is resolved.

Order: every setting is declared once (SETTINGS) -> a value resolves from, most specific first, a
lab's own row, the platform-wide row, its environment variable, then the code default (resolve,
resolve_all) -> admins change it with an audit entry (set_value, reset_value). adapter_thresholds
gathers the speech-adaptation gates' five numbers.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.cache import request
from radreport.cache.keys import GLOBAL, key
from radreport.core.logging import get_logger
from radreport.core.types import ActorType

log = get_logger(__name__)

Source = Literal["lab", "platform", "environment", "default"]


class SettingRefused(ValueError):
    """A value outside a setting's type or bounds, or an unknown key."""


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    label: str
    kind: type[int] | type[float]
    default: float
    env_var: str
    minimum: float
    maximum: float
    description: str
    group: str = "Speech adaptation gates"

    def parse(self, raw: object) -> float | int:
        """A typed, bounded value, or SettingRefused."""
        try:
            value = self.kind(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise SettingRefused(f"{self.key} must be a {'whole number' if self.kind is int else 'number'}") from exc
        if self.kind is int and isinstance(raw, float) and not raw.is_integer():
            raise SettingRefused(f"{self.key} must be a whole number")
        if not self.minimum <= value <= self.maximum:
            raise SettingRefused(f"{self.key} must be between {self.minimum:g} and {self.maximum:g}")
        return value


SETTINGS: dict[str, SettingSpec] = {
    s.key: s
    for s in (
        SettingSpec("adapter.global_hours", "Hours for a global adapter", float, 20.0, "ADAPTER_HOURS_THRESHOLD", 1.0, 2000.0, "G1_volume: usable verbatim hours a lab-wide ASR adapter needs."),
        SettingSpec("adapter.speaker_hours", "Hours for a per-speaker adapter", float, 5.0, "SPEAKER_ADAPTER_HOURS_THRESHOLD", 0.5, 500.0, "G1_volume: usable verbatim hours one radiologist's adapter needs."),
        SettingSpec("adapter.min_speakers", "Minimum distinct speakers", int, 5, "MIN_SPEAKERS_FOR_ADAPTER", 1, 500, "G3_speaker_balance: dictating radiologists a global adapter must hear from."),
        SettingSpec("adapter.max_speaker_share", "Largest speaker's share", float, 0.40, "MAX_SPEAKER_SHARE", 0.05, 1.0, "G3_speaker_balance: the most of the corpus hours one speaker may hold."),
        SettingSpec("adapter.min_device_class_share", "Dominant microphone share", float, 0.90, "MIN_DEVICE_CLASS_SHARE", 0.5, 1.0, "G4_hardware_homogeneous: the share of hours that must come from one kind of capture device."),
    )
}


@dataclass(frozen=True, slots=True)
class Resolved:
    key: str
    value: float | int
    source: Source
    platform_value: float | int | None
    lab_value: float | int | None


def _spec(name: str) -> SettingSpec:
    spec = SETTINGS.get(name)
    if spec is None:
        raise SettingRefused(f"unknown setting {name!r}; known: {', '.join(sorted(SETTINGS))}")
    return spec


def _rows(session: Session, tenant_id: uuid.UUID | None) -> dict[tuple[str, str], Any]:
    """Every stored value visible here, read once per request: `(scope, key) -> value`."""
    from radreport.db.models.ops import SystemConfig

    def load() -> dict[tuple[str, str], Any]:
        query = select(SystemConfig.tenant_id, SystemConfig.key, SystemConfig.value)
        query = query.where(SystemConfig.tenant_id.is_(None) | (SystemConfig.tenant_id == tenant_id)) if tenant_id else query.where(SystemConfig.tenant_id.is_(None))
        return {("lab" if t else "platform", k): v for t, k, v in session.execute(query).all()}

    return request.request_cached(key("system_config", tenant_id or GLOBAL), load)


def _env(spec: SettingSpec) -> float | int | None:
    raw = os.environ.get(spec.env_var)
    if raw is None or raw.strip() == "":
        return None
    try:
        return spec.parse(raw)
    except SettingRefused:
        log.warning("system_config_env_ignored", key=spec.key, env_var=spec.env_var, reason="out of range or not a number")
        return None


def resolve(session: Session, name: str, *, tenant_id: uuid.UUID | None = None) -> Resolved:
    """A setting's effective value for a lab (or platform-wide), and where it came from."""
    spec = _spec(name)
    rows = _rows(session, tenant_id)
    lab = rows.get(("lab", name))
    platform = rows.get(("platform", name))
    lab_value = spec.parse(lab) if lab is not None else None
    platform_value = spec.parse(platform) if platform is not None else None
    if lab_value is not None:
        return Resolved(name, lab_value, "lab", platform_value, lab_value)
    if platform_value is not None:
        return Resolved(name, platform_value, "platform", platform_value, None)
    env = _env(spec)
    if env is not None:
        return Resolved(name, env, "environment", None, None)
    return Resolved(name, spec.kind(spec.default), "default", None, None)


def resolve_all(session: Session, *, tenant_id: uuid.UUID | None = None) -> list[Resolved]:
    return [resolve(session, name, tenant_id=tenant_id) for name in SETTINGS]


def set_value(session: Session, name: str, raw: object, *, actor_id: uuid.UUID, tenant_id: uuid.UUID | None = None) -> Resolved:
    """Store a value platform-wide, or for one lab, with an audit entry."""
    from radreport.db.models.ops import SystemConfig
    from radreport.db.models.orchestration import AuditLog
    from radreport.db.session import bind_tenant

    spec = _spec(name)
    value = spec.parse(raw)
    if tenant_id is not None:
        bind_tenant(session, tenant_id)
    row = session.execute(select(SystemConfig).where(SystemConfig.key == name, SystemConfig.tenant_id == tenant_id if tenant_id else SystemConfig.tenant_id.is_(None))).scalar_one_or_none()
    before = row.value if row else None
    if row is None:
        row = SystemConfig(tenant_id=tenant_id, key=name, value=value, updated_by=actor_id)
        session.add(row)
    else:
        row.value, row.updated_by = value, actor_id
        row.updated_at = dt.datetime.now(dt.UTC)
    session.add(AuditLog(tenant_id=tenant_id, actor_id=actor_id, actor_type=ActorType.USER, action="system_config_set", entity_type="system_config", entity_id=None, before={"key": name, "value": before}, after={"key": name, "value": value}))
    session.flush()
    request.forget(key("system_config", tenant_id or GLOBAL))
    log.info("system_config_set", key=name, value=value, scope=str(tenant_id) if tenant_id else "platform")
    return resolve(session, name, tenant_id=tenant_id)


def reset_value(session: Session, name: str, *, actor_id: uuid.UUID, tenant_id: uuid.UUID | None = None) -> Resolved:
    """Remove a stored value, so the next level down applies again."""
    from radreport.db.models.ops import SystemConfig
    from radreport.db.models.orchestration import AuditLog
    from radreport.db.session import bind_tenant

    _spec(name)
    if tenant_id is not None:
        bind_tenant(session, tenant_id)
    row = session.execute(select(SystemConfig).where(SystemConfig.key == name, SystemConfig.tenant_id == tenant_id if tenant_id else SystemConfig.tenant_id.is_(None))).scalar_one_or_none()
    if row is not None:
        session.add(AuditLog(tenant_id=tenant_id, actor_id=actor_id, actor_type=ActorType.USER, action="system_config_reset", entity_type="system_config", entity_id=None, before={"key": name, "value": row.value}, after={"key": name, "value": None}))
        session.delete(row)
        session.flush()
    request.forget(key("system_config", tenant_id or GLOBAL))
    return resolve(session, name, tenant_id=tenant_id)


@dataclass(frozen=True, slots=True)
class AdapterThresholds:
    """The numbers the speech-adaptation gates compare against."""

    global_hours: float = 20.0
    speaker_hours: float = 5.0
    min_speakers: int = 5
    max_speaker_share: float = 0.40
    min_device_class_share: float = 0.90


def adapter_thresholds(session: Session, *, tenant_id: uuid.UUID | None = None) -> AdapterThresholds:
    """The gates' thresholds as they apply to one lab."""
    value = {name: resolve(session, name, tenant_id=tenant_id).value for name in SETTINGS if name.startswith("adapter.")}
    return AdapterThresholds(global_hours=float(value["adapter.global_hours"]), speaker_hours=float(value["adapter.speaker_hours"]), min_speakers=int(value["adapter.min_speakers"]), max_speaker_share=float(value["adapter.max_speaker_share"]), min_device_class_share=float(value["adapter.min_device_class_share"]))
