"""Allows the template_parse task in model assignments and evaluation runs.

Order: upgrade replaces the task_key checks on task_model_assignment, task_model_assignment_log and
eval_run with ones that include template_parse; downgrade restores the earlier list, refusing while
any row uses the new task.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

TABLES = ("task_model_assignment", "task_model_assignment_log", "eval_run")
BEFORE = ("asr_primary", "asr_secondary", "extraction", "self_correction", "verification", "routing_pick", "utterance_classification", "routing_shortlist", "roundtrip_check", "compose")
AFTER = (*BEFORE, "template_parse")


def _has_constraint(name: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}).first())


def _replace(values: tuple[str, ...]) -> None:
    rendered = ", ".join(f"'{v}'" for v in values)
    for table in TABLES:
        name = f"ck_{table}_task_key_valid"
        if _has_constraint(name):
            op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, f"task_key IN ({rendered})")


def upgrade() -> None:
    _replace(AFTER)


def downgrade() -> None:
    for table in TABLES:
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} WHERE task_key = 'template_parse' LIMIT 1")).first():
            raise RuntimeError(f"{table} still has template_parse rows; retire them before downgrading")
    _replace(BEFORE)
