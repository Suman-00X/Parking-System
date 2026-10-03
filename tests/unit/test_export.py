"""Building the HL7 v2 ORU^R01 message and the FHIR R4 report."""

from __future__ import annotations

import base64
import datetime as dt
import uuid

import pytest

from radreport.export.fhir import STATUS_AMENDED, STATUS_FINAL, FhirContext, build_bundle, build_diagnostic_report
from radreport.export.hl7 import ExportRefused, OruContext, build_oru, escape, hl7_timestamp

SIGNED_AT = dt.datetime(2026, 9, 25, 10, 30, tzinfo=dt.UTC)


def _oru_context(**kw) -> OruContext:
    defaults = dict(accession_number="ACC-1001", patient_id="MRN-77", patient_family_name="Iyer", patient_given_name="A", patient_sex="F", study_description="USG Abdomen", modality="US", signed_at=SIGNED_AT)
    return OruContext(**{**defaults, **kw})


def _build(text: str = "FINDINGS\nLiver: normal.", **kw):
    return build_oru(context=_oru_context(**kw), report_text=text, content_hash="abc123", report_id=uuid.uuid4(), control_id="CTRL1")


# =========================================================== HL7 v2 ORU ======
def test_delimiters_in_report_text_are_escaped() -> None:
    """An unescaped `|` truncates the segment at the receiving end, which presents as a report that silently loses its second half."""
    message = _build("Liver: 5^6 mm | measured & noted ~ see prior")
    rendered = message.render()

    assert "\\S\\" in rendered  # ^
    assert "\\F\\" in rendered  # |
    assert "\\T\\" in rendered  # &
    assert "\\R\\" in rendered  # ~


def test_the_escape_character_itself_is_escaped_first() -> None:
    """Escaping `\\` after the others would double-escape their replacements."""
    assert escape("a\\b") == "a\\E\\b"
    assert escape("a|b\\c") == "a\\F\\b\\E\\c"


def test_segments_are_carriage_return_delimited() -> None:
    """HL7 v2 is CR-delimited. A strict receiver rejects LF; a lenient one silently mis-parses the last segment."""
    rendered = _build().render()
    assert "\n" not in rendered
    assert rendered.endswith("\r")


def test_the_report_body_travels_as_repeating_obx_segments() -> None:
    """Many receivers cap field length and truncate without an error."""
    message = _build("line one\nline two\nline three")
    obx = [s for s in message.segments if s.startswith("OBX")]

    # Three body lines plus the content-hash segment.
    assert len(obx) == 4
    assert "line two" in obx[1]


def test_the_content_hash_travels_with_the_report() -> None:
    """Makes `final_report` tamper-evident; a receiver that stores this can verify what it holds is what was signed."""
    assert "sha256:abc123" in _build().render()


def test_an_addendum_is_sent_as_a_correction_not_a_new_result() -> None:
    """A new result creates a duplicate report in the RIS instead of amending the original — the exact thing the addendum chain exists to avoid."""
    final = _build()
    correction = _build(is_correction=True)

    assert "|RE|" in final.render()
    assert "|CR|" in correction.render()
    assert correction.render().rstrip("\r").endswith("|C")


def test_export_is_refused_without_an_accession_number() -> None:
    """A placeholder here files the report against the wrong study, which is worse than a message that was never sent."""
    with pytest.raises(ExportRefused) as exc:
        _build(accession_number="")
    assert exc.value.code == "no_accession"


def test_an_empty_report_is_refused() -> None:
    with pytest.raises(ExportRefused):
        build_oru(context=_oru_context(), report_text="   ", content_hash="h", report_id=uuid.uuid4())


def test_the_message_frames_for_mllp() -> None:
    framed = _build().as_mllp()
    assert framed.startswith(b"\x0b")
    assert framed.endswith(b"\x1c\x0d")


def test_timestamps_are_utc_regardless_of_input_zone() -> None:
    naive = dt.datetime(2026, 9, 25, 10, 30)
    aware = dt.datetime(2026, 9, 25, 16, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    assert hl7_timestamp(naive) == "20260925103000"
    assert hl7_timestamp(aware) == "20260925103000"


# ============================================================== FHIR R4 ======
def _fhir(**kw):
    context = FhirContext(**{**dict(accession_number="ACC-1001", patient_reference="Patient/77"), **kw})
    return build_diagnostic_report(context=context, report_text="Liver is normal.", content_hash="abc123", report_id=uuid.uuid4(), structured_payload={"liver": {"value_text": "normal", "assertion_status": "present"}, "effusion": {"value_text": None, "assertion_status": "absent"}, "spleen": {"value_text": None, "assertion_status": "not_assessed"}, "node": {"value_numeric": 3.2, "value_unit": "cm", "assertion_status": "present"}})


def test_the_signed_prose_travels_as_the_presented_form() -> None:
    """The legal record is the text the radiologist signed."""
    resource = _fhir()
    encoded = resource["presentedForm"][0]["data"]
    assert base64.b64decode(encoded).decode() == "Liver is normal."


def test_a_dictated_negative_survives_export() -> None:
    """Keeps presence four-state so "absent" is a claim, not an absence. `compose` renders it; the export must not quietly drop it."""
    contained = {o["id"]: o for o in _fhir()["contained"]}

    assert "effusion" in contained
    assert contained["effusion"]["dataAbsentReason"]["text"] == "negated by the radiologist"


def test_a_field_nobody_addressed_is_not_exported() -> None:
    """Exporting an empty Observation would claim the radiologist considered something they did not."""
    assert "spleen" not in {o["id"] for o in _fhir()["contained"]}


def test_a_measurement_exports_with_its_unit() -> None:
    contained = {o["id"]: o for o in _fhir()["contained"]}
    assert contained["node"]["valueQuantity"] == {"value": 3.2, "unit": "cm"}


def test_an_amendment_is_amended_not_final() -> None:
    """An addendum sent as `final` creates a second report rather than superseding the first."""
    assert _fhir()["status"] == STATUS_FINAL
    amended = _fhir(is_amendment=True, amends_identifier="rep-1")
    assert amended["status"] == STATUS_AMENDED
    assert amended["extension"][0]["valueIdentifier"]["value"] == "rep-1"


def test_the_content_hash_is_an_identifier() -> None:
    identifiers = {i["value"] for i in _fhir()["identifier"]}
    assert "sha256:abc123" in identifiers
    assert "ACC-1001" in identifiers


def test_fhir_export_is_refused_without_an_accession() -> None:
    with pytest.raises(ExportRefused):
        build_diagnostic_report(context=FhirContext(accession_number="", patient_reference="Patient/1"), report_text="x", content_hash="h", report_id=uuid.uuid4())


def test_a_bundle_upserts_rather_than_creating_duplicates() -> None:
    """PUT by id, so re-sending a report updates it instead of creating a second one — the same failure as an addendum sent as a new result."""
    bundle = build_bundle([_fhir(), _fhir()])
    assert bundle["type"] == "transaction"
    assert all(e["request"]["method"] == "PUT" for e in bundle["entry"])
