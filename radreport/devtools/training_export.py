"""Exports radiologists' approvals from the labs given, for fine-tuning the template model.

    python -m radreport.devtools.training_export --tenant <lab id> [--tenant <lab id> ...] --out exports/training

Order: open one lab-scoped session per lab, in turn (lab_sessions) -> export (knowledge/training_data.export) -> print the counts.
Labs that have not turned training.share_approvals on are skipped and listed.
"""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy.orm import Session

from radreport.db.session import tenant_session
from radreport.knowledge.training_data import export


def lab_sessions(tenants: list[uuid.UUID], url: str | None = None) -> Iterator[tuple[uuid.UUID, Session]]:
    """One lab at a time: each session is closed before the next lab's opens."""
    for tenant_id in tenants:
        with tenant_session(tenant_id, url=url) as session:
            yield tenant_id, session


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", action="append", required=True, type=uuid.UUID)
    parser.add_argument("--out", type=Path, default=Path("exports/training"))
    args = parser.parse_args(argv)
    result = export(lab_sessions(args.tenant), args.out)
    print(json.dumps({"folder": str(result.folder), "counts": result.counts, "skipped_labs_without_consent": list(result.skipped_labs)}, indent=2))


if __name__ == "__main__":
    main()
