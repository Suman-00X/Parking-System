"""Builds the FHIR R4 DiagnosticReport, the modern alternative to the HL7 v2 message.

Order: build the report resource (build_diagnostic_report) -> wrap it with the resources it
references (build_bundle).
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from radreport.core.logging import get_logger
from radreport.export.hl7 import ExportRefused

log = get_logger(__name__)

#: LOINC for a diagnostic imaging report.
LOINC_IMAGING_REPORT = "18748-4"
LOINC_SYSTEM = "http://loinc.org"

STATUS_FINAL = "final"
STATUS_AMENDED = "amended"


@dataclass(frozen=True, slots=True)
class FhirContext:
    accession_number: str
    patient_reference: str
    """`Patient/<id>`. A reference, not inline demographics: keeps patient
 identity out of anything this system originates, and the receiving system
 already holds the record."""

    study_description: str = ""
    modality: str = ""
    study_datetime: dt.datetime | None = None
    performer_display: str = ""
    signed_at: dt.datetime | None = None
    is_amendment: bool = False
    amends_identifier: str | None = None
    issuer_system: str = "urn:radreport"


def build_diagnostic_report(*, context: FhirContext, report_text: str, content_hash: str, report_id: uuid.UUID, conclusion: str | None = None, structured_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a FHIR R4 DiagnosticReport for one signed report."""
    if not context.accession_number or not context.accession_number.strip():
        raise ExportRefused("no accession number: the receiving system has nothing to match the report to ", code="no_accession")
    if not report_text.strip():
        raise ExportRefused("refusing to export an empty report", code="empty_report")

    issued = context.signed_at or dt.datetime.now(dt.UTC)
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=dt.UTC)

    resource: dict[str, Any] = {
        "resourceType": "DiagnosticReport",
        "id": str(report_id),
        "identifier": [
            {"system": f"{context.issuer_system}/accession", "value": context.accession_number},
            # The content hash as an identifier, so a receiver can verify that
            # what it holds is what was signed (the tamper evidence).
            {"system": f"{context.issuer_system}/content-hash", "value": f"sha256:{content_hash}"},
        ],
        "status": STATUS_AMENDED if context.is_amendment else STATUS_FINAL,
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "RAD", "display": "Radiology"}]}],
        "code": {"coding": [{"system": LOINC_SYSTEM, "code": LOINC_IMAGING_REPORT, "display": "Diagnostic imaging study"}], "text": context.study_description or "Diagnostic imaging study"},
        "subject": {"reference": context.patient_reference},
        "issued": issued.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
        # The signed prose is the legal record, so it travels as the presented
        # form rather than being re-rendered by the receiver from our fields.
        "presentedForm": [{"contentType": "text/plain", "language": "en", "data": base64.b64encode(report_text.encode("utf-8")).decode("ascii"), "title": context.study_description or "Radiology report"}],
    }

    if context.study_datetime is not None:
        moment = context.study_datetime
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.UTC)
        resource["effectiveDateTime"] = moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    if context.performer_display:
        resource["performer"] = [{"display": context.performer_display}]
    if conclusion:
        resource["conclusion"] = conclusion
    if context.modality:
        resource["category"][0]["coding"].append({"system": "http://dicom.nema.org/resources/ontology/DCM", "code": context.modality, "display": context.modality})
    if context.is_amendment and context.amends_identifier:
        # FHIR has no first-class "amends" link on DiagnosticReport, so the relationship rides as an extension rather than being dropped.
        resource["extension"] = [{"url": f"{context.issuer_system}/StructureDefinition/amends", "valueIdentifier": {"system": f"{context.issuer_system}/report", "value": context.amends_identifier}}]
    if structured_payload:
        resource["contained"] = [_observation(key, value, issued, context) for key, value in sorted(structured_payload.items()) if _has_value(value)]

    log.info("fhir_report_built", report_id=str(report_id), accession=context.accession_number, status=resource["status"], observations=len(resource.get("contained", [])))
    return resource


def _has_value(value: Any) -> bool:
    """Is there a clinical claim here worth exporting?"""
    if not isinstance(value, dict):
        return False
    if any(value.get(k) is not None for k in ("value_text", "value_enum", "value_numeric")):
        return True
    return value.get("assertion_status") in ("present", "absent", "uncertain")


def _observation(key: str, value: dict[str, Any], issued: dt.datetime, context: FhirContext) -> dict[str, Any]:
    """One structured field as a contained Observation."""
    observation: dict[str, Any] = {"resourceType": "Observation", "id": key, "status": "final", "code": {"text": key.replace("_", " ")}, "subject": {"reference": context.patient_reference}, "effectiveDateTime": issued.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")}

    if value.get("value_numeric") is not None:
        observation["valueQuantity"] = {"value": value["value_numeric"], "unit": value.get("value_unit") or ""}
    elif value.get("value_enum"):
        observation["valueCodeableConcept"] = {"text": value["value_enum"]}
    elif value.get("value_text"):
        observation["valueString"] = value["value_text"]

    # keeps presence four-state, and that distinction must survive export: "absent" and "not assessed" are different clinical claims, and collapsing them at the boundary undoes the reason the column is not a boolean.
    assertion = value.get("assertion_status")
    if assertion == "absent":
        observation["dataAbsentReason"] = {"text": "negated by the radiologist"}
    elif assertion == "not_assessed":
        observation["dataAbsentReason"] = {"text": "not assessed"}

    if value.get("laterality") and value["laterality"] != "na":
        observation["bodySite"] = {"text": value["laterality"]}
    return observation


def build_bundle(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """A transaction Bundle, for sending several reports in one call."""
    return {"resourceType": "Bundle", "type": "transaction", "entry": [{"resource": resource, "request": {"method": "PUT", "url": f"DiagnosticReport/{resource['id']}"}} for resource in resources]}
