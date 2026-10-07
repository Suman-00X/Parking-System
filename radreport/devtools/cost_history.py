"""Writes a synthetic history of pipeline runs and their stage costs for one lab, so the cost dashboard has something to show.

Order: refuse outside developer machines (require_local) -> one synthetic study and recording per
run, a pipeline_run and its stage rows per day, with a weekday rhythm and one deliberate spike
(write_history). Runs are marked trigger=backfill so they never pass for clinical traffic.

    python -m radreport.devtools.cost_history --lab sunrise --days 60
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core.types import AudioFormat, CaptureDeviceClass, PipelineTrigger, RunStatus, TaskKey, UserRole
from radreport.db.bulk import bulk_insert
from radreport.db.models.identity import AppUser, Patient, RadiologistProfile, Study
from radreport.db.models.ingestion import Recording
from radreport.db.models.orchestration import PipelineRun, StageExecution
from radreport.db.models.tenancy import Tenant
from radreport.db.session import system_session, tenant_session
from radreport.devtools.local_accounts import require_local

#: (stage, task, typical cost per run in USD, tokens in, tokens out); deterministic stages cost nothing.
STAGES: tuple[tuple[str, str | None, float, int, int], ...] = (("preprocess", None, 0.0, 0, 0), ("asr", TaskKey.ASR_PRIMARY, 0.006, 0, 0), ("normalise", None, 0.0, 0, 0), ("routing", TaskKey.ROUTING_SHORTLIST, 0.002, 1800, 120), ("extract", TaskKey.EXTRACTION, 0.031, 6200, 900), ("verify", TaskKey.ROUNDTRIP_CHECK, 0.009, 2600, 300), ("compose", TaskKey.COMPOSE, 0.012, 3100, 700), ("critical", None, 0.0, 0, 0), ("persist", None, 0.0, 0, 0))


def _radiologist(session: Session, tenant_id: uuid.UUID) -> uuid.UUID:
    profile = session.execute(select(RadiologistProfile.id).where(RadiologistProfile.tenant_id == tenant_id)).scalars().first()
    if profile is not None:
        return profile
    user = AppUser(tenant_id=tenant_id, employee_code=f"SYN-{uuid.uuid4().hex[:6]}", display_name="Dr Synthetic", roles=[UserRole.RADIOLOGIST])
    session.add(user)
    session.flush()
    created = RadiologistProfile(tenant_id=tenant_id, user_id=user.id)
    session.add(created)
    session.flush()
    return created.id


def write_history(tenant_id: uuid.UUID, *, days: int = 60, seed: int = 7, spike_days_ago: int = 3, url: str | None = None, today: dt.date | None = None) -> int:
    """Write `days` of runs ending today; returns how many runs. A lab's day `spike_days_ago` costs several times the usual."""
    rng = random.Random(seed)
    end = today or dt.datetime.now(dt.UTC).date()
    written = 0
    with tenant_session(tenant_id, url=url) as session:
        radiologist_id = _radiologist(session, tenant_id)
        for back in range(days - 1, -1, -1):
            day = end - dt.timedelta(days=back)
            volume = rng.randint(14, 22) if day.weekday() < 5 else rng.randint(3, 7)
            spike = back == spike_days_ago
            patients, studies, recordings, runs, stages = [], [], [], [], []
            for n in range(volume):
                at = dt.datetime.combine(day, dt.time(8 + n % 10, rng.randint(0, 59)), tzinfo=dt.UTC)
                patient_id, study_id, recording_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
                patients.append({"id": patient_id, "tenant_id": tenant_id, "mrn": f"SYNTH-{patient_id.hex[:12]}", "pseudonym": f"PT-{patient_id.hex[12:22]}"})
                studies.append({"id": study_id, "tenant_id": tenant_id, "patient_id": patient_id})
                recordings.append({"id": recording_id, "tenant_id": tenant_id, "study_id": study_id, "radiologist_id": radiologist_id, "object_key": f"synthetic/{recording_id}.flac", "content_hash": uuid.uuid4().hex, "capture_device_class": CaptureDeviceClass.DICTATION_MIC_PTT, "audio_format": AudioFormat.FLAC, "duration_seconds": rng.randint(40, 180), "uploaded_at": at})
                total = 0.0
                for stage_name, task, cost, tokens_in, tokens_out in STAGES:
                    # A spike day: a prompt change that broke caching makes the model stages several times dearer.
                    scale = (4.5 if spike else 1.0) * rng.uniform(0.75, 1.3)
                    spend = round(cost * scale, 4)
                    total += spend
                    cached = 0 if spike or not tokens_in else int(tokens_in * rng.uniform(0.55, 0.8))
                    stages.append({"id": uuid.uuid4(), "tenant_id": tenant_id, "pipeline_run_id": run_id, "stage_name": stage_name, "attempt": 1, "input_ref": {"synthetic": True}, "output_ref": {}, "task_key": task, "tokens_in": tokens_in or None, "tokens_out": tokens_out or None, "cache_read_tokens": cached or None, "cost_usd": spend if task else None, "duration_ms": rng.randint(20, 2400) if task else rng.randint(2, 60), "status": RunStatus.SUCCEEDED, "radiologist_id": radiologist_id, "created_at": at})
                runs.append({"id": run_id, "tenant_id": tenant_id, "recording_id": recording_id, "pipeline_version": "synthetic", "trigger": PipelineTrigger.BACKFILL, "status": RunStatus.SUCCEEDED, "started_at": at, "completed_at": at + dt.timedelta(seconds=rng.randint(20, 90)), "total_cost_usd": round(total, 4), "budget_cap_usd": 0.5, "created_at": at, "updated_at": at})
            bulk_insert(session, Patient, patients)
            bulk_insert(session, Study, studies)
            bulk_insert(session, Recording, recordings)
            bulk_insert(session, PipelineRun, runs)
            bulk_insert(session, StageExecution, stages)
            written += volume
    return written


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lab", required=True, help="the lab's slug")
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args(argv)
    require_local()
    with system_session() as session:
        tenant = session.execute(select(Tenant).where(Tenant.slug == args.lab)).scalar_one_or_none()
        if tenant is None:
            raise SystemExit(f"no lab with slug {args.lab!r}")
        tenant_id = tenant.id
    print(f"wrote {write_history(tenant_id, days=args.days)} synthetic runs for {args.lab}")


if __name__ == "__main__":
    main()
