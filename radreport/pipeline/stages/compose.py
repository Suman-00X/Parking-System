"""Stage 12: turns the filled-in fields into the report's readable text.

Order: render each field (render_value) -> assemble them in template order (compose) into a
ComposedReport.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from radreport.core.logging import get_logger
from radreport.core.types import AssertionStatus, FillSource
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.stages.grounding import renderable
from radreport.pipeline.state import FieldValue, PipelineState

log = get_logger(__name__)

#: Fallback order when `render_spec` names no sections.
DEFAULT_SECTIONS: tuple[str, ...] = ("TECHNIQUE", "FINDINGS", "IMPRESSION")


@dataclass(frozen=True, slots=True)
class RenderSpec:
    """The per-tenant house style, as data rather than an instruction."""

    sections: tuple[str, ...] = DEFAULT_SECTIONS
    field_order: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    sections_by_field: dict[str, str] = field(default_factory=dict)
    heading_suffix: str = ":"
    field_separator: str = "\n"
    omit_empty_sections: bool = True

    @classmethod
    def from_json(cls, spec: dict | None, json_schema: dict | None = None) -> RenderSpec:
        spec = spec or {}
        properties = (json_schema or {}).get("properties") or {}
        return cls(sections=tuple(spec.get("sections") or DEFAULT_SECTIONS), field_order=tuple(spec.get("field_order") or properties.keys()), labels={k: (v.get("title") or k) for k, v in properties.items()}, sections_by_field={k: (v.get("x-section") or "FINDINGS") for k, v in properties.items()}, heading_suffix=spec.get("heading_suffix", ":"), field_separator=spec.get("field_separator", "\n"))


@dataclass(slots=True)
class ComposedReport:
    text: str
    rendered_fields: list[str] = field(default_factory=list)
    omitted_fields: list[str] = field(default_factory=list)
    """Grounded-but-empty, or not extracted at all. Shown as gaps in review — never filled in here."""


def render_value(value: FieldValue) -> str | None:
    """One field's text. Returns None when there is nothing to say."""
    if value.assertion_status == AssertionStatus.NOT_ASSESSED and not (value.value_text or value.value_enum or value.value_numeric is not None):
        return None

    if value.value_text:
        body = value.value_text.strip()
    elif value.value_enum:
        body = value.value_enum
    elif value.value_numeric is not None:
        body = f"{value.value_numeric:g}"
        if value.value_unit:
            body = f"{body} {value.value_unit}"
    elif value.assertion_status == AssertionStatus.ABSENT:
        body = "Not present."
    elif value.assertion_status == AssertionStatus.UNCERTAIN:
        body = "Equivocal."
    else:
        return None

    if value.laterality and value.laterality not in ("na", None):
        if value.laterality not in body.lower():
            body = f"{value.laterality.capitalize()}: {body}"
    return body


def compose(state: PipelineState, spec: RenderSpec) -> ComposedReport:
    """Render grounded atoms into the lab's house format. Pure."""
    values = renderable(state)
    report = ComposedReport(text="")

    ordered = [k for k in spec.field_order if k in values]
    ordered += sorted(k for k in values if k not in spec.field_order)

    by_section: dict[str, list[str]] = {}
    for key in ordered:
        value = values[key]
        body = render_value(value)
        if body is None:
            report.omitted_fields.append(key)
            continue
        label = spec.labels.get(key, key.replace("_", " ").title())
        section = spec.sections_by_field.get(key, "FINDINGS")
        by_section.setdefault(section, []).append(f"{label}{spec.heading_suffix} {body}")
        report.rendered_fields.append(key)

    # Fields the template declares but nothing grounded. Recorded as gaps so
    # the review UI can show them; never rendered with a default.
    report.omitted_fields.extend(k for k in spec.field_order if k not in values and k not in report.omitted_fields)

    blocks: list[str] = []
    seen_sections: list[str] = list(spec.sections)
    seen_sections += [s for s in by_section if s not in seen_sections]
    for section in seen_sections:
        lines = by_section.get(section, [])
        if not lines and spec.omit_empty_sections:
            continue
        blocks.append(f"{section}\n" + spec.field_separator.join(lines))

    report.text = "\n\n".join(blocks)
    return report


class ComposeStage:
    """Deterministic; reads only grounded values."""

    name = "compose"
    version = "1.0.0"

    def __init__(self, spec: RenderSpec | None = None) -> None:
        self._spec = spec or RenderSpec()

    def is_idempotent(self) -> bool:
        return True

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        report = compose(state, self._spec)
        state.rendered_text = report.text

        warnings: list[str] = []
        ungrounded = [k for k, v in state.field_values.items() if not v.is_grounded]
        if ungrounded:
            warnings.append(f"{len(ungrounded)} ungrounded field(s) were not rendered: {', '.join(sorted(ungrounded)[:5])}")
        auto_filled = [k for k, v in state.field_values.items() if v.fill_source in (FillSource.TEMPLATE_DEFAULT, FillSource.BLANKET_NORMAL)]
        if auto_filled:
            # Verification blocks on this too; compose says it again because this is the stage where an auto-filled value would have become indistinguishable from a dictated one in the output text.
            warnings.append(f"auto-filled field(s) present, which V1 does not produce: {', '.join(sorted(auto_filled))}")

        log.info("compose_complete", rendered=len(report.rendered_fields), omitted=len(report.omitted_fields), chars=len(report.text))
        return StageResult(output=state, confidence=1.0, warnings=warnings)
