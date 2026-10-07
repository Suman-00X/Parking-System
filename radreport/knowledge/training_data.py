"""Turns radiologists' approvals into training examples for a lab-tuned template model, from labs that agreed to share them.

Order: check the lab agreed (shares: training.share_approvals) -> collect approved and edited
templates with the text they were read from, as chat examples whose answer is the schema the
radiologist signed off (template_examples) -> collect sound-alike answers and new-term decisions as
labelled pairs (variant_examples, term_examples) -> split each set by a stable hash so the same item
always lands in the same half (split) -> write JSONL files and a manifest (export).
Nothing from a report or a transcript is exported: templates are blank forms, and term contexts stay behind.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core import system_config
from radreport.core.types import CandidateReviewStatus
from radreport.db.models.knowledge import LexiconSurfaceVariant, LexiconTerm, PotentialLexiconTerm
from radreport.db.models.onboarding import TemplateImportCandidate
from radreport.onboarding.template_llm import SCHEMA, SYSTEM

EVAL_SHARE = 0.1
_TYPE_BACK = {"text": "text", "enum": "enum", "measurement": "measurement", "boolean_tri": "boolean", "list": "text"}


def shares(session: Session, tenant_id: uuid.UUID) -> bool:
    return bool(int(system_config.resolve(session, "training.share_approvals", tenant_id=tenant_id).value))


def _fields_answer(schema: dict[str, Any]) -> dict[str, Any]:
    """The approved schema in the shape the template model answers in."""
    props = sorted((schema.get("properties") or {}).items(), key=lambda kv: kv[1].get("x-seq", 0))
    fields = []
    sections: list[str] = []
    for _key, prop in props:
        section = prop.get("x-section") or "FINDINGS"
        if section not in sections:
            sections.append(section)
        item: dict[str, Any] = {"label": prop.get("title") or _key, "section": section, "data_type": _TYPE_BACK.get(prop.get("x-data-type", "text"), "text")}
        if prop.get("enum"):
            item["enum_values"] = list(prop["enum"])
        if prop.get("x-unit"):
            item["unit"] = prop["x-unit"]
        fields.append(item)
    return {"title": schema.get("title") or "", "sections": sections, "fields": fields}


def template_examples(session: Session, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = session.execute(select(TemplateImportCandidate).where(TemplateImportCandidate.tenant_id == tenant_id, TemplateImportCandidate.review_status.in_((CandidateReviewStatus.APPROVED, CandidateReviewStatus.EDITED)), TemplateImportCandidate.source_text.isnot(None)).order_by(TemplateImportCandidate.id)).scalars()
    out = []
    for c in rows:
        answer = _fields_answer(c.proposed_json_schema or {})
        if not answer["fields"]:
            continue
        out.append({"id": f"template:{c.id}", "messages": [{"role": "system", "content": SYSTEM + "\nAnswer with JSON matching: " + json.dumps(SCHEMA)}, {"role": "user", "content": c.source_text}, {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)}], "meta": {"review_status": c.review_status, "parser_confidence": float(c.parse_confidence) if c.parse_confidence is not None else None}})
    return out


def variant_examples(session: Session, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    """Only what a radiologist answered; automatic approvals are the model's own guesses, not labels."""
    rows = session.execute(select(LexiconSurfaceVariant, LexiconTerm.canonical_form, LexiconTerm.term_type).join(LexiconTerm, LexiconTerm.id == LexiconSurfaceVariant.lexicon_term_id).where(LexiconSurfaceVariant.tenant_id == tenant_id, LexiconSurfaceVariant.decided_by.isnot(None), LexiconSurfaceVariant.review_status.in_(("approved", "rejected"))).order_by(LexiconSurfaceVariant.id)).all()
    return [{"id": f"variant:{v.id}", "heard": v.surface_text, "term": form, "term_type": kind, "label": "same" if v.review_status == "approved" else "different", "confidence": float(v.confidence) if v.confidence is not None else None} for v, form, kind in rows]


def term_examples(session: Session, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = session.execute(select(PotentialLexiconTerm).where(PotentialLexiconTerm.tenant_id == tenant_id, PotentialLexiconTerm.status.in_(("approved", "rejected"))).order_by(PotentialLexiconTerm.id)).scalars()
    return [{"id": f"term:{t.id}", "term": t.surface_text, "term_type": t.term_type, "frequency": t.frequency, "label": "term" if t.status == "approved" else "not_a_term"} for t in rows]


def split(example_id: str) -> str:
    """train or eval, fixed by the id so re-exports never move an item between halves."""
    return "eval" if int(hashlib.sha256(example_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < EVAL_SHARE else "train"


@dataclass(frozen=True, slots=True)
class ExportResult:
    folder: Path
    counts: dict[str, dict[str, int]]
    skipped_labs: tuple[str, ...]


def export(sessions: Iterable[tuple[uuid.UUID, Session]], folder: Path) -> ExportResult:
    """Write <set>.<train|eval>.jsonl for every set, from every lab that shares, and a manifest."""
    folder.mkdir(parents=True, exist_ok=True)
    collected: dict[str, list[dict[str, Any]]] = {"template_parse": [], "variant_pairs": [], "new_terms": []}
    skipped: list[str] = []
    labs = []
    for tenant_id, session in sessions:
        if not shares(session, tenant_id):
            skipped.append(str(tenant_id))
            continue
        labs.append(str(tenant_id))
        collected["template_parse"] += template_examples(session, tenant_id)
        collected["variant_pairs"] += variant_examples(session, tenant_id)
        collected["new_terms"] += term_examples(session, tenant_id)
    counts: dict[str, dict[str, int]] = {}
    for name, rows in collected.items():
        halves: dict[str, list[dict[str, Any]]] = {"train": [], "eval": []}
        for row in rows:
            halves[split(row["id"])].append(row)
        counts[name] = {}
        for half, items in halves.items():
            (folder / f"{name}.{half}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in items), encoding="utf-8")
            counts[name][half] = len(items)
    (folder / "manifest.json").write_text(json.dumps({"exported_at": dt.datetime.now(dt.UTC).isoformat(), "labs": labs, "skipped_labs_without_consent": skipped, "counts": counts, "eval_share": EVAL_SHARE}, indent=2))
    return ExportResult(folder=folder, counts=counts, skipped_labs=tuple(skipped))
