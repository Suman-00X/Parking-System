"""Builds the HL7 v2 ORU^R01 message, the format most radiology practices' existing systems already accept.

Order: assemble the message from the signed report (build_oru), escaping field text and
formatting timestamps as the standard requires (escape, hl7_timestamp). It refuses to build a
message with no accession number rather than send a placeholder.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from radreport.core.logging import get_logger

log = get_logger(__name__)

#: MSH-1 and MSH-2. The classic encoding characters.
FIELD_SEP = "|"
ENCODING_CHARS = "^~\\&"

#: HL7's escape sequences. Applied to every user-supplied value, because a
#: radiologist writing "5^6 mm" or a patient named "O'Brien|Jr" is ordinary.
_ESCAPES: tuple[tuple[str, str], ...] = (
    ("\\", "\\E\\"),  # first: the escape character itself
    ("|", "\\F\\"),
    ("^", "\\S\\"),
    ("~", "\\R\\"),
    ("&", "\\T\\"),
)

#: OBX-11 result status. `F` final, `C` correction to a final result.
STATUS_FINAL = "F"
STATUS_CORRECTED = "C"


class ExportRefused(Exception):
    """The message could not be built correctly, so none was built."""

    def __init__(self, reason: str, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


def escape(value: str | None) -> str:
    """Escape HL7 delimiters. An unescaped `|` truncates the segment."""
    if not value:
        return ""
    out = str(value)
    for char, replacement in _ESCAPES:
        out = out.replace(char, replacement)
    return out.replace("\r", " ").replace("\n", " ")


def hl7_timestamp(moment: dt.datetime) -> str:
    """`YYYYMMDDHHMMSS`, in UTC and explicitly so."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).strftime("%Y%m%d%H%M%S")


@dataclass(frozen=True, slots=True)
class OruContext:
    """Everything an ORU^R01 needs that is not the report text itself."""

    accession_number: str
    """`OBR-3`, the filler order number the RIS matches on."""

    patient_id: str
    patient_family_name: str
    patient_given_name: str = ""
    patient_sex: str = "U"
    patient_dob: dt.date | None = None

    study_description: str = ""
    modality: str = ""
    study_datetime: dt.datetime | None = None

    signed_by_name: str = ""
    signed_at: dt.datetime | None = None
    ordering_provider: str = ""

    sending_application: str = "RADREPORT"
    sending_facility: str = ""
    receiving_application: str = "RIS"
    receiving_facility: str = ""

    is_correction: bool = False
    """An addendum."""

    placer_order_number: str = ""


@dataclass(slots=True)
class Hl7Message:
    segments: list[str] = field(default_factory=list)
    control_id: str = ""

    def render(self) -> str:
        """Segments joined by CR."""
        return "\r".join(self.segments) + "\r"

    def as_mllp(self) -> bytes:
        """Wrapped for Minimal Lower Layer Protocol transport."""
        return b"\x0b" + self.render().encode("utf-8") + b"\x1c\x0d"


def build_oru(*, context: OruContext, report_text: str, content_hash: str, report_id: uuid.UUID, control_id: str | None = None, now: dt.datetime | None = None) -> Hl7Message:
    """Build an ORU^R01 for one signed report."""
    if not context.accession_number or not context.accession_number.strip():
        raise ExportRefused("no accession number: the RIS matches results on OBR-3, and a placeholder files the report against the wrong study ( is still open on where this value comes from in upload metadata)", code="no_accession")
    if not report_text.strip():
        raise ExportRefused("refusing to export an empty report", code="empty_report")

    now = now or dt.datetime.now(dt.UTC)
    control_id = control_id or uuid.uuid4().hex[:20]
    status = STATUS_CORRECTED if context.is_correction else STATUS_FINAL

    message = Hl7Message(control_id=control_id)
    message.segments.append(_msh(context, control_id, now))
    message.segments.append(_pid(context))
    message.segments.append(_orc(context, status))
    message.segments.append(_obr(context, status, now))

    # One OBX per line. Many receivers cap field length and truncate silently,
    # so a long impression arriving as repeating segments is the safe shape.
    for index, line in enumerate(report_text.splitlines() or [report_text], start=1):
        message.segments.append(FIELD_SEP.join(["OBX", str(index), "TX", "^".join(["RADRPT", "Radiology Report", "L"]), "1", escape(line), "", "", "", "", status]))

    # The content hash travels with the report.
    message.segments.append(FIELD_SEP.join(["OBX", str(len(report_text.splitlines()) + 1), "ST", "^".join(["RADRPT-HASH", "Report content hash", "L"]), "1", escape(f"sha256:{content_hash}"), "", "", "", "", status]))

    log.info("hl7_oru_built", report_id=str(report_id), accession=context.accession_number, control_id=control_id, is_correction=context.is_correction, segments=len(message.segments))
    return message


def _msh(context: OruContext, control_id: str, now: dt.datetime) -> str:
    # MSH is the one segment where field 1 *is* the separator, so the joined
    # list starts after it.
    return FIELD_SEP.join(["MSH", ENCODING_CHARS, escape(context.sending_application), escape(context.sending_facility), escape(context.receiving_application), escape(context.receiving_facility), hl7_timestamp(now), "", "ORU^R01^ORU_R01", control_id, "P", "2.5.1"])


def _pid(context: OruContext) -> str:
    name = "^".join([escape(context.patient_family_name), escape(context.patient_given_name)])
    dob = context.patient_dob.strftime("%Y%m%d") if context.patient_dob else ""
    return FIELD_SEP.join(["PID", "1", "", escape(context.patient_id), "", name, "", dob, escape(context.patient_sex)])


def _orc(context: OruContext, status: str) -> str:
    # `RE` observations to follow; `CR` a corrected result.
    control = "CR" if context.is_correction else "RE"
    return FIELD_SEP.join(["ORC", control, escape(context.placer_order_number), escape(context.accession_number)])


def _obr(context: OruContext, status: str, now: dt.datetime) -> str:
    observation_time = hl7_timestamp(context.study_datetime) if context.study_datetime else ""
    signed = hl7_timestamp(context.signed_at) if context.signed_at else hl7_timestamp(now)
    universal_service = "^".join([escape(context.modality), escape(context.study_description), "L"])
    return FIELD_SEP.join(["OBR", "1", escape(context.placer_order_number), escape(context.accession_number), universal_service, "", "", observation_time, "", "", "", "", "", "", "", "", escape(context.ordering_provider), "", "", "", "", signed, "", "", status])
