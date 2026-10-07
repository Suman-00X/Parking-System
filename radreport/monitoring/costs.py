"""What the pipeline costs, per lab and per stage, and the days that cost far more than usual.

Order: read daily and per-stage spend across labs (daily_costs, stage_costs, through the
tenant_daily_cost and tenant_stage_cost functions) -> flag spikes against a trailing window
(detect_spikes) -> assemble the platform picture (platform_summary) or one lab's (lab_summary) ->
on a schedule, announce new spikes as cost.anomaly events (scan_for_anomalies, the
cost_anomaly_scan job).
"""

from __future__ import annotations

import datetime as dt
import statistics
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.db import sharding
from radreport.db.models.tenancy import Tenant

log = get_logger(__name__)

#: A day is a spike when it beats both: SPIKE_RATIO x the trailing median, and the trailing mean + SPIKE_SIGMAS standard deviations.
SPIKE_WINDOW_DAYS = 14
SPIKE_RATIO = 2.0
SPIKE_SIGMAS = 3.0
#: Ignore spikes smaller than this in absolute terms; a jump from 2 cents to 6 is noise, not a bill.
SPIKE_MIN_USD = 1.0
#: A lab needs this many days with runs in the window before any day can count as a spike: ramping up is not an anomaly.
SPIKE_MIN_ACTIVE_DAYS = 7


@dataclass(frozen=True, slots=True)
class Spike:
    day: dt.date
    cost_usd: float
    baseline_usd: float
    ratio: float

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "day": self.day.isoformat()}


@dataclass(slots=True)
class LabCost:
    tenant_id: uuid.UUID
    name: str
    cost_usd: float = 0.0
    runs: int = 0
    previous_cost_usd: float = 0.0
    failed_runs: int = 0
    budget_hits: int = 0
    spikes: list[Spike] = field(default_factory=list)

    @property
    def avg_cost_usd(self) -> float:
        return self.cost_usd / self.runs if self.runs else 0.0

    @property
    def change(self) -> float | None:
        """Spend against the previous period of the same length; None when there was none."""
        return (self.cost_usd - self.previous_cost_usd) / self.previous_cost_usd if self.previous_cost_usd else None


def _window(days: int, today: dt.date | None) -> tuple[dt.date, dt.date]:
    end = today or dt.datetime.now(dt.UTC).date()
    return end - dt.timedelta(days=days - 1), end


def daily_costs(session: Session, *, start: dt.date, end: dt.date, tenant_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    """Spend per lab per day; with sharding on, from every shard, since each holds its own labs' runs."""
    if sharding.shard_map().enabled:
        return sharding.fan_out(lambda shard: _daily(shard, start=start, end=end, tenant_id=tenant_id))
    return _daily(session, start=start, end=end, tenant_id=tenant_id)


def _daily(session: Session, *, start: dt.date, end: dt.date, tenant_id: uuid.UUID | None) -> list[dict[str, Any]]:
    rows = session.execute(text("SELECT tenant_id, day, run_count, total_cost_usd, avg_cost_usd, max_cost_usd, failed_runs, budget_hits FROM tenant_daily_cost(:a, :b, :t)"), {"a": start, "b": end, "t": tenant_id}).all()
    return [{"tenant_id": r[0], "day": r[1], "runs": int(r[2]), "cost_usd": float(r[3] or 0), "avg_cost_usd": float(r[4] or 0), "max_cost_usd": float(r[5] or 0), "failed_runs": int(r[6]), "budget_hits": int(r[7])} for r in rows]


def stage_costs(session: Session, *, start: dt.date, end: dt.date, tenant_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    """Spend per lab per stage, from every shard when sharding is on."""
    if sharding.shard_map().enabled:
        return sharding.fan_out(lambda shard: _stages(shard, start=start, end=end, tenant_id=tenant_id))
    return _stages(session, start=start, end=end, tenant_id=tenant_id)


def _stages(session: Session, *, start: dt.date, end: dt.date, tenant_id: uuid.UUID | None) -> list[dict[str, Any]]:
    rows = session.execute(text("SELECT tenant_id, stage_name, task_key, executions, cost_usd, tokens_in, tokens_out, cache_read_tokens, avg_duration_ms, failures FROM tenant_stage_cost(:a, :b, :t)"), {"a": start, "b": end, "t": tenant_id}).all()
    out = []
    for r in rows:
        tokens_in, cache_read = int(r[5] or 0), int(r[7] or 0)
        out.append({"tenant_id": r[0], "stage_name": r[1], "task_key": r[2], "executions": int(r[3]), "cost_usd": float(r[4] or 0), "tokens_in": tokens_in, "tokens_out": int(r[6] or 0), "cache_read_tokens": cache_read, "cache_hit_ratio": round(cache_read / (tokens_in + cache_read), 4) if tokens_in + cache_read else 0.0, "avg_duration_ms": round(float(r[8] or 0), 1), "failures": int(r[9])})
    return out


def series(rows: list[dict[str, Any]], *, start: dt.date, end: dt.date) -> list[tuple[dt.date, float]]:
    """Daily spend from `start` to `end`, a zero for every day with no runs."""
    by_day: dict[dt.date, float] = {}
    for row in rows:
        by_day[row["day"]] = by_day.get(row["day"], 0.0) + row["cost_usd"]
    days = (end - start).days + 1
    return [(start + dt.timedelta(days=i), round(by_day.get(start + dt.timedelta(days=i), 0.0), 6)) for i in range(days)]


def detect_spikes(points: list[tuple[dt.date, float]], *, window: int = SPIKE_WINDOW_DAYS, ratio: float = SPIKE_RATIO, sigmas: float = SPIKE_SIGMAS, min_usd: float = SPIKE_MIN_USD, min_active_days: int = SPIKE_MIN_ACTIVE_DAYS) -> list[Spike]:
    """Days whose spend is far above the trailing window's; the first `window` days only serve as history."""
    spikes: list[Spike] = []
    for i in range(window, len(points)):
        day, cost = points[i]
        history = [c for _, c in points[i - window : i]]
        if sum(1 for c in history if c > 0) < min_active_days:
            continue
        median = statistics.median(history)
        mean = statistics.fmean(history)
        spread = statistics.pstdev(history)
        if cost >= min_usd and cost > ratio * median and cost > mean + sigmas * spread:
            spikes.append(Spike(day=day, cost_usd=round(cost, 4), baseline_usd=round(median, 4), ratio=round(cost / median, 2) if median else float("inf")))
    return spikes


def _lab_names(session: Session) -> dict[uuid.UUID, str]:
    return {t.id: t.name for t in session.execute(select(Tenant)).scalars().all()}


def platform_summary(session: Session, *, days: int = 30, today: dt.date | None = None) -> dict[str, Any]:
    """Every lab's spend over the period, the previous period for comparison, the daily trend, and the spikes."""
    start, end = _window(days, today)
    history_start = start - dt.timedelta(days=max(days, SPIKE_WINDOW_DAYS))
    rows = daily_costs(session, start=history_start, end=end)
    names = _lab_names(session)
    labs: dict[uuid.UUID, LabCost] = {}
    previous_start = start - dt.timedelta(days=days)
    for row in rows:
        lab = labs.setdefault(row["tenant_id"], LabCost(tenant_id=row["tenant_id"], name=names.get(row["tenant_id"], "unknown lab")))
        if row["day"] >= start:
            lab.cost_usd += row["cost_usd"]
            lab.runs += row["runs"]
            lab.failed_runs += row["failed_runs"]
            lab.budget_hits += row["budget_hits"]
        elif row["day"] >= previous_start:
            lab.previous_cost_usd += row["cost_usd"]
    for tenant_id, lab in labs.items():
        lab_series = series([r for r in rows if r["tenant_id"] == tenant_id], start=history_start, end=end)
        lab.spikes = [s for s in detect_spikes(lab_series) if s.day >= start]
    platform_series = series(rows, start=history_start, end=end)
    platform_spikes = [s for s in detect_spikes(platform_series) if s.day >= start]
    total = sum(lab.cost_usd for lab in labs.values())
    runs = sum(lab.runs for lab in labs.values())
    previous = sum(lab.previous_cost_usd for lab in labs.values())
    ranked = sorted((lab for lab in labs.values() if lab.cost_usd or lab.runs), key=lambda lab: lab.cost_usd, reverse=True)
    return {"start": start, "end": end, "days": days, "total_cost_usd": round(total, 4), "previous_cost_usd": round(previous, 4), "runs": runs, "avg_cost_per_report_usd": round(total / runs, 4) if runs else 0.0, "change": (total - previous) / previous if previous else None, "series": [p for p in platform_series if p[0] >= start], "spikes": platform_spikes, "labs": ranked, "stages": _merge_stages(stage_costs(session, start=start, end=end))}


def _merge_stages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = merged.setdefault(row["stage_name"], {"stage_name": row["stage_name"], "task_key": row["task_key"], "executions": 0, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "cache_read_tokens": 0, "failures": 0, "duration_total": 0.0})
        for name in ("executions", "cost_usd", "tokens_in", "tokens_out", "cache_read_tokens", "failures"):
            entry[name] += row[name]
        entry["duration_total"] += row["avg_duration_ms"] * row["executions"]
    out = []
    for entry in merged.values():
        reads, fresh = entry["cache_read_tokens"], entry["tokens_in"]
        out.append({**{k: v for k, v in entry.items() if k != "duration_total"}, "cost_usd": round(entry["cost_usd"], 6), "avg_duration_ms": round(entry["duration_total"] / entry["executions"], 1) if entry["executions"] else 0.0, "cache_hit_ratio": round(reads / (reads + fresh), 4) if reads + fresh else 0.0})
    return sorted(out, key=lambda e: (-e["cost_usd"], -e["executions"]))


def lab_summary(session: Session, tenant_id: uuid.UUID, *, days: int = 30, today: dt.date | None = None) -> dict[str, Any]:
    """One lab: its daily trend, what each stage costs, how well prompt caching works, and its spikes."""
    start, end = _window(days, today)
    history_start = start - dt.timedelta(days=max(days, SPIKE_WINDOW_DAYS))
    rows = daily_costs(session, start=history_start, end=end, tenant_id=tenant_id)
    lab_series = series(rows, start=history_start, end=end)
    current = [r for r in rows if r["day"] >= start]
    previous = sum(r["cost_usd"] for r in rows if start - dt.timedelta(days=days) <= r["day"] < start)
    total = sum(r["cost_usd"] for r in current)
    runs = sum(r["runs"] for r in current)
    return {"tenant_id": tenant_id, "start": start, "end": end, "days": days, "total_cost_usd": round(total, 4), "previous_cost_usd": round(previous, 4), "runs": runs, "avg_cost_per_report_usd": round(total / runs, 4) if runs else 0.0, "max_run_cost_usd": max((r["max_cost_usd"] for r in current), default=0.0), "failed_runs": sum(r["failed_runs"] for r in current), "budget_hits": sum(r["budget_hits"] for r in current), "change": (total - previous) / previous if previous else None, "series": [p for p in lab_series if p[0] >= start], "spikes": [s for s in detect_spikes(lab_series) if s.day >= start], "stages": _merge_stages(stage_costs(session, start=start, end=end, tenant_id=tenant_id))}


def scan_for_anomalies(session: Session, *, today: dt.date | None = None, url: str | None = None) -> list[dict[str, Any]]:
    """Announce each lab's new spend spikes once, as cost.anomaly events in that lab."""
    from radreport.db.models.events import OutboxEvent
    from radreport.db.session import tenant_session
    from radreport.events.outbox import Topic, emit

    summary = platform_summary(session, days=SPIKE_WINDOW_DAYS, today=today)
    announced: list[dict[str, Any]] = []
    for lab in summary["labs"]:
        for spike in lab.spikes:
            with tenant_session(lab.tenant_id, url=url) as lab_session:
                seen = lab_session.execute(select(OutboxEvent.id).where(OutboxEvent.tenant_id == lab.tenant_id, OutboxEvent.topic == Topic.COST_ANOMALY, OutboxEvent.payload["day"].astext == spike.day.isoformat())).first()
                if seen:
                    continue
                emit(lab_session, Topic.COST_ANOMALY, {**spike.as_dict(), "lab": lab.name}, tenant_id=lab.tenant_id)
            log.warning("cost_spike_detected", tenant_id=str(lab.tenant_id), day=spike.day.isoformat(), cost_usd=spike.cost_usd, baseline_usd=spike.baseline_usd)
            announced.append({"tenant_id": str(lab.tenant_id), **spike.as_dict()})
    return announced
