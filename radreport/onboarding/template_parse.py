"""Reads an uploaded .docx or plain-text reporting template into fields a reviewer can check.

Order: pull the paragraphs out of the file (extract_paragraphs) -> split them into labelled
fields (parse_template) -> infer each field's shape and section (infer_structure).
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

from radreport.core.types import FieldDataType

#: Word's paragraph and text nodes. Namespace-agnostic: matching on the local
#: name avoids carrying the full WordprocessingML namespace map for two tags.
_PARA = re.compile(rb"<w:p[ >].*?</w:p>|<w:p/>", re.DOTALL)
_TEXT = re.compile(rb"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.DOTALL)
_STYLE = re.compile(rb'<w:pStyle\s+w:val="([^"]*)"')
_XML_ENTITY = {b"&amp;": b"&", b"&lt;": b"<", b"&gt;": b">", b"&quot;": b'"', b"&apos;": b"'"}

#: "FINDINGS:", "IMPRESSION" — a heading is short, terminal-colon or all-caps.
_SECTION_LINE = re.compile(r"^([A-Z][A-Z /&-]{2,40}):?\s*$")
#: "Liver: normal in size" — a field is `Label: value`.
_FIELD_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /()'-]{1,60}?)\s*:\s*(.*)$")
#: "[normal/enlarged/shrunken]" or "(normal | enlarged)" — an enum in the wild.
_ENUM_HINT = re.compile(r"[\[(]([^\])]*[/|][^\])]*)[\])]")
#: "3.2 cm", "12 mm" — a measurement slot.
_MEASUREMENT_HINT = re.compile(r"\b\d+(?:\.\d+)?\s*(cm|mm|ml|cc|hu)\b", re.IGNORECASE)
_BLANK_SLOT = re.compile(r"_{3,}|\.{4,}|\bX{2,}\b")


@dataclass(frozen=True, slots=True)
class ParsedField:
    field_key: str
    display_label: str
    section: str
    data_type: str
    seq: int
    enum_values: tuple[str, ...] = ()
    unit: str | None = None
    sample_text: str | None = None
    """The literal text that followed the label; boilerplate ranking mines default-normal candidates from these, and template import never promotes one."""


@dataclass(slots=True)
class ParsedTemplate:
    title: str
    sections: list[str] = field(default_factory=list)
    fields: list[ParsedField] = field(default_factory=list)
    modality: str | None = None
    body_region: str | None = None
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)


class UnsupportedDocument(ValueError):
    """The file is not a format template import can parse."""


def extract_paragraphs(data: bytes, filename: str) -> list[str]:
    """Recover ordered paragraph text from a .docx or .txt upload."""
    lowered = filename.lower()
    if lowered.endswith(".docx"):
        return _docx_paragraphs(data)
    if lowered.endswith((".txt", ".md")):
        return [line.rstrip() for line in data.decode("utf-8", errors="replace").splitlines()]
    if lowered.endswith(".pdf"):
        raise UnsupportedDocument("PDF template import is not implemented: extracting field structure from PDF text layout silently mangles labels, and a mangled template_field is invisible after import. Re-export as .docx.")
    raise UnsupportedDocument(f"unsupported template format: {filename!r} (: Word only)")


def _docx_paragraphs(data: bytes) -> list[str]:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise UnsupportedDocument(f"not a readable .docx: {exc}") from exc

    paragraphs: list[str] = []
    for match in _PARA.finditer(xml):
        block = match.group(0)
        runs = b"".join(_TEXT.findall(block))
        for entity, char in _XML_ENTITY.items():
            runs = runs.replace(entity, char)
        text = runs.decode("utf-8", errors="replace").strip()
        style = _STYLE.search(block)
        # A Word heading carries its level in the style name, not the text. Keep
        # it as an explicit marker so `_infer_structure` need not guess twice.
        if style and b"Heading" in style.group(1) and text:
            paragraphs.append(f"\x00HEADING\x00{text}")
        elif text:
            paragraphs.append(text)
    return paragraphs


def parse_template(data: bytes, filename: str) -> ParsedTemplate:
    """Parse one uploaded document into a template candidate."""
    paragraphs = extract_paragraphs(data, filename)
    return infer_structure(paragraphs, fallback_title=_title_from_filename(filename))


def _title_from_filename(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return re.sub(r"[_-]+", " ", stem).strip() or "Untitled template"


def infer_structure(paragraphs: list[str], *, fallback_title: str) -> ParsedTemplate:
    """Turn ordered paragraphs into sections and fields."""
    parsed = ParsedTemplate(title=fallback_title)
    current_section = "FINDINGS"
    seq = 0
    seen_keys: set[str] = set()
    heading_count = 0

    for index, raw in enumerate(paragraphs):
        is_marked_heading = raw.startswith("\x00HEADING\x00")
        line = raw.removeprefix("\x00HEADING\x00").strip()
        if not line:
            continue

        # The first marked heading is the template's name, not a section.
        if is_marked_heading and index == 0:
            parsed.title = line
            heading_count += 1
            continue

        section_match = _SECTION_LINE.match(line)
        if is_marked_heading or section_match:
            current_section = (section_match.group(1) if section_match else line).strip().upper()
            if current_section not in parsed.sections:
                parsed.sections.append(current_section)
            heading_count += 1
            continue

        field_match = _FIELD_LINE.match(line)
        if not field_match:
            continue

        label, value = field_match.group(1).strip(), field_match.group(2).strip()
        key = _field_key(label)
        if key in seen_keys:
            parsed.warnings.append(f"duplicate field label {label!r}; kept the first occurrence")
            continue
        seen_keys.add(key)

        if current_section not in parsed.sections:
            parsed.sections.append(current_section)

        data_type, enum_values, unit = _infer_type(value)
        seq += 1
        parsed.fields.append(ParsedField(field_key=key, display_label=label, section=current_section, data_type=data_type, seq=seq, enum_values=enum_values, unit=unit, sample_text=value or None))

    parsed.modality, parsed.body_region = _infer_modality_and_region(parsed.title)
    parsed.confidence = _confidence(parsed, heading_count)
    if not parsed.fields:
        parsed.warnings.append("no 'Label: value' lines found; the document may be free prose")
    if not parsed.modality:
        parsed.warnings.append("modality could not be inferred from the title")
    return parsed


def _field_key(label: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return key or "field"


def _infer_type(value: str) -> tuple[str, tuple[str, ...], str | None]:
    """Guess a field's data type from the sample value beside it."""
    enum_match = _ENUM_HINT.search(value)
    if enum_match:
        options = tuple(opt.strip() for opt in re.split(r"[/|]", enum_match.group(1)) if opt.strip())
        if len(options) >= 2:
            return FieldDataType.ENUM, options, None

    measurement = _MEASUREMENT_HINT.search(value)
    if measurement:
        return FieldDataType.MEASUREMENT, (), measurement.group(1).lower()

    return FieldDataType.TEXT, (), None


_MODALITIES: tuple[tuple[str, tuple[str, ...]], ...] = (("CT", ("ct", "computed tomography", "cect", "ncct")), ("MRI", ("mri", "mr ", "magnetic resonance")), ("US", ("ultrasound", "usg", "sonography", "doppler")), ("XR", ("x-ray", "xray", "radiograph", "chest pa")), ("MG", ("mammogram", "mammography")))

_REGIONS: tuple[tuple[str, tuple[str, ...]], ...] = (("chest", ("chest", "thorax", "lung", "pulmonary")), ("abdomen", ("abdomen", "abdominal", "liver", "kidney", "renal", "pelvis kub")), ("head", ("head", "brain", "cranial", "skull")), ("spine", ("spine", "lumbar", "cervical", "dorsal")), ("pelvis", ("pelvis", "pelvic", "obstetric", "uterus")), ("extremity", ("knee", "shoulder", "ankle", "wrist", "hip", "elbow")), ("neck", ("neck", "thyroid", "carotid")))


def _infer_modality_and_region(title: str) -> tuple[str | None, str | None]:
    lowered = f" {title.lower()} "
    modality = next((code for code, hints in _MODALITIES if _any_hint(lowered, hints)), None)
    region = next((code for code, hints in _REGIONS if _any_hint(lowered, hints)), None)
    return modality, region


def _any_hint(haystack: str, hints: tuple[str, ...]) -> bool:
    return any(hint in haystack for hint in hints)


def _confidence(parsed: ParsedTemplate, heading_count: int) -> float:
    """A blunt 0–1 score. Low means "make the radiologist read every field"."""
    if not parsed.fields:
        return 0.0

    score = 0.35
    score += min(0.25, 0.05 * len(parsed.sections))
    score += min(0.20, 0.02 * len(parsed.fields))
    if heading_count:
        score += 0.10
    if parsed.modality:
        score += 0.05
    if parsed.body_region:
        score += 0.05
    typed = sum(1 for f in parsed.fields if f.data_type != FieldDataType.TEXT)
    score += min(0.10, 0.02 * typed)
    blanks = sum(1 for f in parsed.fields if f.sample_text and _BLANK_SLOT.search(f.sample_text))
    if blanks:
        score -= min(0.15, 0.03 * blanks)
    return round(max(0.0, min(1.0, score)), 4)
