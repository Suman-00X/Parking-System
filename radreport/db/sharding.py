"""Places each lab on one of several databases, and keeps the lab's own row present on the database that holds its data.

Order: a consistent-hash ring with virtual nodes maps a lab id to a shard (HashRing), so adding a
shard moves only about 1/N of labs -> an explicit pin in the directory database overrides the ring
for a lab that must stay put or be isolated (pin_lab, lab_shard) -> sessions for a lab connect to
its shard (url_for) -> after a commit that changed a lab's row, the row is copied to the other
database that holds it (sync_tenant_rows, installed by install_tenant_sync) -> cross-lab reads ask
every shard and merge (fan_out).

Sharding is off until RADREPORT_DB__SHARDS names more than one database; then the directory
database (RADREPORT_DATABASE_URL) holds platform tables, the lab list and the pins, and each shard
holds a full schema with its labs' rows.
"""

from __future__ import annotations

import bisect
import hashlib
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from radreport.core.config import get_settings
from radreport.core.logging import get_logger

log = get_logger(__name__)

#: Points per shard on the ring. More points spread labs more evenly; 128 keeps every shard within a few percent of its share.
VIRTUAL_NODES = 128


def _point(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big")


class HashRing:
    """A lab id hashes to a point; the first shard point clockwise from it owns the lab."""

    def __init__(self, shards: Iterable[str], *, vnodes: int = VIRTUAL_NODES) -> None:
        names = sorted(set(shards))
        if not names:
            raise ValueError("a ring needs at least one shard")
        self.shards = tuple(names)
        pairs = sorted((_point(f"{name}#{i}"), name) for name in names for i in range(vnodes))
        self._points = [p for p, _ in pairs]
        self._owners = [n for _, n in pairs]

    def shard_for(self, lab_id: uuid.UUID | str) -> str:
        index = bisect.bisect(self._points, _point(str(lab_id))) % len(self._points)
        return self._owners[index]


@dataclass(frozen=True, slots=True)
class ShardMap:
    """Shard name -> database URL, and the ring over them."""

    urls: dict[str, str]
    ring: HashRing

    @property
    def enabled(self) -> bool:
        return len(self.urls) > 1


_lock = threading.Lock()
_map: ShardMap | None = None
_pins: dict[uuid.UUID, str] = {}


def shard_map() -> ShardMap:
    """The configured shards; a single implicit shard, the directory database, when none are."""
    global _map
    with _lock:
        if _map is None:
            settings = get_settings()
            urls = dict(settings.db.shards) or {"primary": settings.database_url}
            _map = ShardMap(urls=urls, ring=HashRing(urls))
        return _map


def reset() -> None:
    """Forget the cached map and pins (tests, and after changing shard settings)."""
    global _map
    with _lock:
        _map = None
        _pins.clear()


def lab_shard(lab_id: uuid.UUID) -> str:
    """The shard a lab lives on: its pin if it has one, else where the ring puts it."""
    current = shard_map()
    if not current.enabled:
        return next(iter(current.urls))
    pinned = _pins.get(lab_id)
    if pinned is None:
        from radreport.db.models.ops import LabShard
        from radreport.db.session import get_sessionmaker

        with get_sessionmaker()() as session:
            pinned = session.execute(select(LabShard.shard_name).where(LabShard.lab_id == lab_id)).scalar_one_or_none() or ""
        _pins[lab_id] = pinned
    return pinned if pinned in current.urls else current.ring.shard_for(lab_id)


def url_for(lab_id: uuid.UUID | None) -> str | None:
    """The database URL a lab's session should use; None means the directory database."""
    current = shard_map()
    if not current.enabled or lab_id is None:
        return None
    return current.urls[lab_shard(lab_id)]


def _normal(url: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(url).render_as_string(hide_password=False)


def on_lab_shard(session: Session, lab_id: uuid.UUID) -> bool:
    """Whether `session` is connected to the database that holds this lab's rows."""
    target = url_for(lab_id)
    return target is None or _normal(target) == session.get_bind().url.render_as_string(hide_password=False)


def pin_lab(session: Session, lab_id: uuid.UUID, shard_name: str, *, reason: str) -> None:
    """Hold a lab on one shard whatever the ring says, e.g. a large lab given a database of its own."""
    from radreport.db.models.ops import LabShard

    if shard_name not in shard_map().urls:
        raise ValueError(f"no shard named {shard_name!r}")
    row = session.get(LabShard, lab_id)
    if row is None:
        session.add(LabShard(lab_id=lab_id, shard_name=shard_name, reason=reason))
    else:
        row.shard_name, row.reason = shard_name, reason
    session.flush()
    _pins[lab_id] = shard_name


def moves_if_added(lab_ids: Iterable[uuid.UUID], new_shard: str) -> list[uuid.UUID]:
    """The labs a new shard would take over: what a rebalance has to copy."""
    current = shard_map()
    grown = HashRing([*current.urls, new_shard])
    return [lab for lab in lab_ids if not _pins.get(lab) and grown.shard_for(lab) != current.ring.shard_for(lab)]


def fan_out[T](fn: Callable[[Session], list[T]]) -> list[T]:
    """Run a read on every shard (the directory database alone when sharding is off) and concatenate the results."""
    from radreport.db.session import read_session_on

    out: list[T] = []
    for url in dict.fromkeys(shard_map().urls.values()):
        with read_session_on(url) as session:
            out.extend(fn(session))
    return out


# ------------------------------------------------------------ row sync -----
_TENANT_COLUMNS = ("id", "name", "slug", "status", "training_pooling_consent", "training_consent_ref", "patient_notice_version", "consent_effective_from", "consent_withdrawn_at", "created_at", "updated_at")


def sync_tenant_rows(source_url: str | None, tenant_ids: Iterable[uuid.UUID]) -> None:
    """Copy lab rows from the database just committed to the other one that holds them (directory or shard)."""
    from radreport.db.session import get_engine

    current = shard_map()
    if not current.enabled:
        return
    directory = get_settings().database_url
    source = source_url or directory
    for lab_id in tenant_ids:
        shard_url = current.urls[lab_shard(lab_id)]
        target = shard_url if source == directory else directory
        if target == source:
            continue
        with get_engine(source).connect() as conn:
            row = conn.execute(text(f"SELECT {', '.join(_TENANT_COLUMNS)} FROM tenant WHERE id = :id"), {"id": lab_id}).mappings().first()
        if row is None:
            continue
        assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in _TENANT_COLUMNS if c != "id")
        with get_engine(target).begin() as conn:
            conn.execute(text(f"INSERT INTO tenant ({', '.join(_TENANT_COLUMNS)}) VALUES ({', '.join(':' + c for c in _TENANT_COLUMNS)}) ON CONFLICT (id) DO UPDATE SET {assignments}"), dict(row))
        log.info("tenant_row_synced", tenant_id=str(lab_id), to="shard" if target != directory else "directory")


def _remember_tenant_changes(session: Session, _context: Any) -> None:
    from radreport.db.models.tenancy import Tenant

    # After the flush, so a new lab already has its id.
    changed = {obj.id for obj in (*session.new, *session.dirty) if isinstance(obj, Tenant) and obj.id is not None}
    if changed:
        session.info.setdefault("tenants_changed", set()).update(changed)


def _sync_after_commit(session: Session) -> None:
    changed = session.info.pop("tenants_changed", None)
    if changed:
        bind = session.get_bind()
        sync_tenant_rows(bind.url.render_as_string(hide_password=False), changed)


_installed = False


def install_tenant_sync() -> None:
    """Listen on every session: a committed change to a lab's row is copied to its other database."""
    global _installed
    if _installed:
        return
    event.listen(Session, "after_flush", _remember_tenant_changes)
    event.listen(Session, "after_commit", _sync_after_commit)
    _installed = True
