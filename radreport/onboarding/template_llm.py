"""Has a small language model read a template document the regular parser was unsure of, and keeps only what the document actually says.

Order: decide whether a parse needs help (needs_fallback, against templates.llm_fallback_below) ->
find the lab's template_parse model, if one is assigned (fallback_for) -> ask it for the fields as
JSON (read_with_model) -> drop every field whose label is not in the document, so nothing invented
reaches a radiologist (_grounded) -> merge with the parser's own fields, the parser winning where
both found a label (merge). Any failure leaves the parser's result as it was, with a warning.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.adapters.llm.base import LLMClient, LLMRequest, LLMResponse
from radreport.adapters.llm.prompt import PromptBundle, VolatileBlock, schema_block, system_block
from radreport.core import system_config
from radreport.core.errors import ModelResolutionError
from radreport.core.logging import get_logger
from radreport.core.types import FieldDataType, TaskKey
from radreport.onboarding.template_parse import ParsedField, ParsedTemplate, _field_key, _infer_modality_and_region

log = get_logger(__name__)

SYSTEM = """You read radiology reporting templates. List every field a radiologist fills in: the organ, structure or
measurement named, the section it sits under, and its type. Use the document's own words for every label;
never add a field the document does not mention. Types: text, enum (give the options the document lists),
measurement (give the unit), boolean. Sections are upper case, e.g. FINDINGS, IMPRESSION."""

SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": False, "required": ["title", "sections", "fields"], "properties": {"title": {"type": "string"}, "sections": {"type": "array", "items": {"type": "string"}}, "fields": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["label", "section", "data_type"], "properties": {"label": {"type": "string"}, "section": {"type": "string"}, "data_type": {"type": "string", "enum": ["text", "enum", "measurement", "boolean"]}, "enum_values": {"type": "array", "items": {"type": "string"}}, "unit": {"type": "string"}, "sample_text": {"type": "string"}}}}}}
#: The most text sent to the model; templates are short, and a long upload is not a template.
MAX_CHARS = 12_000
#: A merged parse that the model helped with is never scored above this: a radiologist still reads every field.
MERGED_CONFIDENCE_CAP = 0.85
_TYPES = {"text": FieldDataType.TEXT, "enum": FieldDataType.ENUM, "measurement": FieldDataType.MEASUREMENT, "boolean": FieldDataType.BOOLEAN_TRI}


@dataclass(slots=True)
class FallbackOutcome:
    parsed: ParsedTemplate
    used: bool = False
    model_id: str | None = None
    fields_added: int = 0
    fields_dropped: int = 0
    """Labels the model gave that are not in the document."""

    latency_ms: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    def audit(self) -> dict[str, Any]:
        return {"used": self.used, "model": self.model_id, "fields_added": self.fields_added, "fields_dropped": self.fields_dropped, "latency_ms": self.latency_ms, "cost_usd": round(self.cost_usd, 6), "error": self.error}


#: What submit_templates calls: the paragraphs and the parser's result in, the model's reply out.
Fallback = Callable[[list[str], ParsedTemplate], Awaitable[LLMResponse]]


def needs_fallback(session: Session, tenant_id: uuid.UUID, parsed: ParsedTemplate) -> bool:
    return parsed.confidence < float(system_config.resolve(session, "templates.llm_fallback_below", tenant_id=tenant_id).value)


def prompt_for(paragraphs: list[str], title: str) -> LLMRequest:
    text = "\n".join(p.removeprefix("\x00HEADING\x00") for p in paragraphs)[:MAX_CHARS]
    bundle = PromptBundle(stable=[system_block(SYSTEM), schema_block(json.dumps(SCHEMA))], volatile=[VolatileBlock(text=f"Template file: {title}\n\n{text}", label="document")])
    return LLMRequest(prompt=bundle, max_tokens=2048, temperature=0.0, seed=7, json_schema=SCHEMA, metadata={"task": TaskKey.TEMPLATE_PARSE})


def model_fallback(client: LLMClient, model_id: str) -> Fallback:
    """A Fallback that calls one model."""

    async def call(paragraphs: list[str], parsed: ParsedTemplate) -> LLMResponse:
        return await client.complete(prompt_for(paragraphs, parsed.title), model_id=model_id)

    return call


def fallback_for(session: Session, tenant_id: uuid.UUID) -> Fallback | None:
    """The lab's assigned template_parse model as a Fallback, or None when the lab has none."""
    from radreport.adapters.llm.factory import client_for
    from radreport.adapters.llm.registry import TaskModelResolver
    from radreport.db.models.modelconfig import ModelProvider

    try:
        resolved = TaskModelResolver(session)._load(TaskKey.TEMPLATE_PARSE, tenant_id)
    except ModelResolutionError:
        return None
    env_var = session.execute(select(ModelProvider.api_key_env_var).where(ModelProvider.name == resolved.ref.provider_name)).scalars().first()
    try:
        return model_fallback(client_for(resolved.ref, api_key_env_var=env_var), resolved.ref.model_identifier)
    except ModelResolutionError as exc:
        log.warning("template_fallback_unavailable", tenant_id=str(tenant_id), reason=str(exc))
        return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _grounded(label: str, document: str) -> bool:
    """Every word of the label appears in the document (ignoring case and punctuation)."""
    words = _norm(label).split()
    return bool(words) and all(re.search(rf"\b{re.escape(w)}", document) for w in words)


def fields_from_reply(reply: dict[str, Any], paragraphs: list[str]) -> tuple[list[ParsedField], list[str], int]:
    """(grounded fields, sections, how many were dropped as not in the document)."""
    document = _norm(" ".join(paragraphs))
    out: list[ParsedField] = []
    seen: set[str] = set()
    dropped = 0
    sections: list[str] = []
    for item in reply.get("fields") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:60]
        if not label or not _grounded(label, document):
            dropped += 1
            continue
        key = _field_key(label)
        if key in seen:
            continue
        seen.add(key)
        section = (str(item.get("section") or "FINDINGS").strip().upper() or "FINDINGS")[:40]
        if section not in sections:
            sections.append(section)
        data_type = _TYPES.get(str(item.get("data_type")), FieldDataType.TEXT)
        options = tuple(str(o).strip() for o in item.get("enum_values") or () if str(o).strip())[:20]
        if data_type == FieldDataType.ENUM and len(options) < 2:
            data_type, options = FieldDataType.TEXT, ()
        unit = str(item.get("unit") or "").strip().lower()[:8] or None
        out.append(ParsedField(field_key=key, display_label=label, section=section, data_type=data_type, seq=len(out) + 1, enum_values=options if data_type == FieldDataType.ENUM else (), unit=unit if data_type == FieldDataType.MEASUREMENT else None, sample_text=str(item.get("sample_text") or "").strip()[:200] or None))
    return out, sections, dropped


def merge(regex: ParsedTemplate, model_fields: list[ParsedField], model_sections: list[str]) -> tuple[ParsedTemplate, int]:
    """The parser's fields, then the model's that the parser missed; (merged, how many the model added)."""
    keys = {f.field_key for f in regex.fields}
    added = [f for f in model_fields if f.field_key not in keys]
    fields = list(regex.fields) + added
    fields = [ParsedField(field_key=f.field_key, display_label=f.display_label, section=f.section, data_type=f.data_type, seq=i + 1, enum_values=f.enum_values, unit=f.unit, sample_text=f.sample_text) for i, f in enumerate(fields)]
    sections = list(regex.sections) + [s for s in model_sections if s not in regex.sections and any(f.section == s for f in added)]
    modality, region = regex.modality, regex.body_region
    if not modality or not region:
        guessed = _infer_modality_and_region(regex.title)
        modality, region = modality or guessed[0], region or guessed[1]
    merged = ParsedTemplate(title=regex.title, sections=sections, fields=fields, modality=modality, body_region=region, warnings=[w for w in regex.warnings if not w.startswith("no 'Label: value' lines")])
    # More fields found is more confidence, but a model-read parse never looks as sure as a clean structured one.
    merged.confidence = round(min(MERGED_CONFIDENCE_CAP, max(regex.confidence, 0.5 + 0.3 * min(1.0, len(fields) / 8))), 4) if fields else regex.confidence
    if added:
        merged.warnings.append(f"{len(added)} field(s) read by the template model; check each one")
    return merged, len(added)


def _run(coro: Awaitable[LLMResponse]) -> LLMResponse:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    raise RuntimeError("the template fallback runs from synchronous code, not inside an event loop")


def apply_fallback(paragraphs: list[str], parsed: ParsedTemplate, fallback: Fallback) -> FallbackOutcome:
    """Ask the model and merge its grounded fields; on any failure keep the parser's result."""
    try:
        reply = _run(fallback(paragraphs, parsed))
    except Exception as exc:  # noqa: BLE001 - a model outage must not fail an upload
        parsed.warnings.append("the template model could not be reached; only the parser's fields are shown")
        log.warning("template_fallback_failed", error=str(exc)[:200])
        return FallbackOutcome(parsed=parsed, error=str(exc)[:200])
    payload = reply.structured
    if payload is None:
        try:
            payload = json.loads(reply.text)
        except (json.JSONDecodeError, TypeError):
            parsed.warnings.append("the template model's reply was not valid JSON; only the parser's fields are shown")
            return FallbackOutcome(parsed=parsed, model_id=reply.model_id, latency_ms=reply.latency_ms, cost_usd=reply.cost_usd, error="invalid_json")
    model_fields, sections, dropped = fields_from_reply(payload if isinstance(payload, dict) else {}, paragraphs)
    merged, added = merge(parsed, model_fields, sections)
    return FallbackOutcome(parsed=merged, used=True, model_id=reply.model_id, fields_added=added, fields_dropped=dropped, latency_ms=reply.latency_ms, cost_usd=reply.cost_usd)
