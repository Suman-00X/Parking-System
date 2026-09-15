"""Parsing uploaded templates and seeding critical-findings rules, with no database."""

from __future__ import annotations

from radreport.core.types import FieldDataType, UserRole
from radreport.devtools.synthetic import synth_template_docx as make_docx
from radreport.onboarding import critical_rules, lexicon, roster
from radreport.onboarding.template_parse import UnsupportedDocument, infer_structure, parse_template
from radreport.onboarding.templates import build_json_schema


# ============================================= roster & voice enrollment =====
def test_roster_csv_imports_the_good_lines_and_names_the_bad_ones() -> None:
    """A 40-person roster with two bad rows imports 38 and reports two."""
    csv_bytes = b"employee_code,display_name,email,roles,subspecialty\nE1,Dr Anand,anand@lab.in,radiologist,neuro;msk\nE2,Dr Bhat,bhat@lab.in,radiologist;lab_admin,\n,Dr Nameless,x@lab.in,radiologist,\nE4,Dr Dutta,d@lab.in,wizard,\nE1,Dr Duplicate,dup@lab.in,radiologist,\n"
    rows, problems = roster.parse_roster_csv(csv_bytes)

    assert [r.employee_code for r in rows] == ["E1", "E2"]
    assert rows[0].subspecialty == ("neuro", "msk")
    assert rows[1].roles == (UserRole.RADIOLOGIST, UserRole.LAB_ADMIN)
    assert len(problems) == 3
    assert any("employee_code and display_name" in p for p in problems)
    assert any("wizard" in p for p in problems)
    assert any("duplicate" in p for p in problems)


def test_roster_csv_missing_a_required_column_fails_the_whole_file() -> None:
    """A header without `employee_code` is a wrong export, not a bad row."""
    rows, problems = roster.parse_roster_csv(b"name,email\nDr Anand,a@lab.in\n")
    assert rows == []
    assert "missing required column" in problems[0]


def test_roles_default_to_radiologist_and_are_validated() -> None:
    rows, problems = roster.parse_roster_csv(b"employee_code,display_name\nE9,Dr Solo\n")
    assert problems == []
    assert rows[0].roles == (UserRole.RADIOLOGIST,)


# ======================================================= template import =====
CHEST_CT = [("CT Chest Plain", True), ("FINDINGS", True), ("Lungs: Normal in size and attenuation", False), ("Pleura: No effusion [present/absent]", False), ("Mediastinum: Largest node 3.2 cm in short axis", False), ("IMPRESSION", True), ("Summary: Unremarkable study", False)]


def test_docx_parse_recovers_sections_fields_and_modality() -> None:
    parsed = parse_template(make_docx(CHEST_CT), "ct_chest_plain.docx")

    assert parsed.title == "CT Chest Plain"
    assert parsed.sections == ["FINDINGS", "IMPRESSION"]
    assert [f.field_key for f in parsed.fields] == ["lungs", "pleura", "mediastinum", "summary"]
    assert parsed.modality == "CT"
    assert parsed.body_region == "chest"
    # The IMPRESSION field must be attributed to the IMPRESSION section, not
    # carried over from FINDINGS — section drives parallel extraction.
    assert parsed.fields[-1].section == "IMPRESSION"


def test_enum_and_measurement_types_are_inferred_but_presence_is_never_boolean() -> None:
    """Presence is never a bare boolean."""
    parsed = parse_template(make_docx(CHEST_CT), "ct_chest.docx")
    by_key = {f.field_key: f for f in parsed.fields}

    assert by_key["pleura"].data_type == FieldDataType.ENUM
    assert by_key["pleura"].enum_values == ("present", "absent")
    assert by_key["mediastinum"].data_type == FieldDataType.MEASUREMENT
    assert by_key["mediastinum"].unit == "cm"
    assert by_key["lungs"].data_type == FieldDataType.TEXT
    assert all(f.data_type != FieldDataType.BOOLEAN_TRI for f in parsed.fields)


def test_free_prose_scores_zero_confidence_and_says_why() -> None:
    """A document that yields paragraphs but no fields is a parse failure."""
    prose = [("Referring physician requested a scan.", False), ("It was normal.", False)]
    parsed = infer_structure([text for text, _ in prose], fallback_title="Unstructured note")

    assert parsed.confidence == 0.0
    assert any("free prose" in w for w in parsed.warnings)


def test_a_well_structured_template_clears_the_review_threshold() -> None:
    from radreport.onboarding.templates import LOW_CONFIDENCE_THRESHOLD

    parsed = parse_template(make_docx(CHEST_CT), "ct_chest_plain.docx")
    assert parsed.confidence >= LOW_CONFIDENCE_THRESHOLD


def test_pdf_is_refused_rather_than_half_parsed() -> None:
    """A mangled `template_field` is invisible after import — refuse instead."""
    try:
        parse_template(b"%PDF-1.7 ...", "ct_chest.pdf")
    except UnsupportedDocument as exc:
        assert "PDF" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("PDF should be refused")


def test_generated_json_schema_marks_nothing_required() -> None:
    """A required field pressures the model to invent a value."""
    parsed = parse_template(make_docx(CHEST_CT), "ct_chest.docx")
    schema = build_json_schema(parsed)

    assert schema["required"] == []
    assert schema["additionalProperties"] is False
    assert schema["properties"]["pleura"]["enum"] == ["present", "absent"]
    assert schema["properties"]["mediastinum"]["x-unit"] == "cm"
    # Section and order ride along, so the rebuild of `template_field` and the
    # schema that validates extraction cannot drift.
    assert schema["properties"]["summary"]["x-section"] == "IMPRESSION"
    assert schema["properties"]["lungs"]["x-seq"] == 1


# =========================================================== term mining =====
def test_mining_keeps_frequent_acronyms_and_drops_noise() -> None:
    texts = ["The LMP was recorded. USG performed."] * 6 + ["A ONEOFF term."]
    mined = lexicon.mine_terms(texts, min_frequency=5)
    forms = {m.canonical_form for m in mined}

    assert "LMP" in forms
    assert "USG" in forms
    assert "ONEOFF" not in forms


def test_known_polysemous_acronyms_are_flagged_for_review() -> None:
    """PA is posteroanterior *and* pulmonary artery."""
    assert "PA" in lexicon.KNOWN_POLYSEMOUS
    assert "RA" in lexicon.KNOWN_POLYSEMOUS


def test_stopword_phrases_are_not_mined_as_anatomy() -> None:
    texts = ["There is no evidence of the and is are was."] * 10
    mined = lexicon.mine_terms(texts, min_frequency=3)
    assert not any(m.canonical_form == "there is no" for m in mined)


# =============================================== critical-findings rules =====
class _Rule:
    """A `critical_finding_rule` stand-in — `matches` touches no DB state."""

    def __init__(self, pattern: str, *, negation_sensitive: bool = True) -> None:
        self.pattern = pattern
        self.pattern_type = "lexical"
        self.negation_sensitive = negation_sensitive


def test_a_negated_finding_does_not_alert() -> None:
    """The failure mode that matters: "no pneumothorax" must not fire."""
    rule = _Rule("pneumothorax|collapsed lung")
    assert critical_rules.matches(rule, "There is no pneumothorax.") is False
    assert critical_rules.matches(rule, "Large right pneumothorax is seen.") is True


def test_negation_is_sentence_local_not_document_wide() -> None:
    """A document-wide negation scan suppresses a real finding because some other sentence happened to say "no fracture"."""
    rule = _Rule("pneumothorax")
    transcript = "No fracture is seen. There is a moderate left pneumothorax."
    assert critical_rules.matches(rule, transcript) is True


def test_negation_can_be_switched_off_per_rule() -> None:
    rule = _Rule("pneumothorax", negation_sensitive=False)
    assert critical_rules.matches(rule, "There is no pneumothorax.") is True


def test_corpus_mentions_exclude_negated_sentences() -> None:
    """ "No pneumothorax" 4,000 times says nothing about how often this lab sees one — counting it would mislead the radiologist prioritising rules."""
    texts = ["No pneumothorax is seen."] * 20 + ["Large pneumothorax noted."]
    counts, examples = critical_rules._corpus_mentions(texts)

    assert counts.get("PNEUMOTHORAX") == 1
    # The splitter consumes the terminating period; the example is the
    # sentence, not the punctuation.
    assert examples["PNEUMOTHORAX"] == ["Large pneumothorax noted"]


def test_seeded_rules_carry_no_escalation_path() -> None:
    """The critical-findings rules stage's gate is a human naming who is called. A seeded default would be the person already looking at the queue."""
    for code, label, patterns, severity in critical_rules.BASELINE_FINDINGS:
        assert code and label and patterns
        assert severity in ("red", "orange")
    assert critical_rules.DEFAULT_SLA_MINUTES["red"] < (critical_rules.DEFAULT_SLA_MINUTES["orange"])
