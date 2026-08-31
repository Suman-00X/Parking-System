"""The four deterministic safety stages. Each one exists to stop a specific named failure, and these tests name it."""

from __future__ import annotations

import uuid

import pytest

from radreport.core.types import AlertSeverity, AssertionStatus, CheckType, FillSource, Laterality, PatternType, Severity, UtteranceLabel
from radreport.pipeline.stages.confidence import compute_confidence, needs_radiologist
from radreport.pipeline.stages.critical import detect_alerts, highest_severity
from radreport.pipeline.stages.grounding import ground_field_values, quote_is_verbatim, renderable
from radreport.pipeline.stages.providers import CriticalRuleEntry
from radreport.pipeline.stages.repairs import apply_repairs, detect_repairs
from radreport.pipeline.stages.verify import run_checks
from radreport.pipeline.state import FieldValue, FindingSketch, PipelineState, ProvenanceRef, TranscriptState, Utterance, VerificationFindingState


def _state(text: str = "", **kwargs) -> PipelineState:
    return PipelineState(tenant_id=uuid.uuid4(), recording_id=uuid.uuid4(), pipeline_run_id=uuid.uuid4(), transcript=TranscriptState(text=text) if text else None, **kwargs)


def _utt(seq: int, text: str, start: int, **kwargs) -> Utterance:
    return Utterance(seq=seq, char_start=start, char_end=start + len(text), audio_start_ms=seq * 1000, audio_end_ms=(seq + 1) * 1000, text=text, **kwargs)


# ================================================ self-corrections ====
def test_left_sorry_right_retracts_the_left_not_the_right() -> None:
    """The canonical G4 failure. Naive filtering keeps `left`."""
    utterances = [_utt(0, "there is a cyst in the left kidney", 0), _utt(1, "sorry, the right kidney", 40)]
    repairs = detect_repairs(utterances)
    apply_repairs(utterances, repairs)

    assert len(repairs) == 1
    assert repairs[0].retracted_seq == 0
    assert repairs[0].correcting_seq == 1
    assert repairs[0].changes_high_stakes_term is True

    assert utterances[0].is_included_downstream is False
    assert utterances[0].superseded_by_seq == 1
    assert utterances[0].label == UtteranceLabel.SELF_CORRECTION
    # The correction survives intact.
    assert utterances[1].is_included_downstream is True


def test_a_retracted_span_is_marked_never_deleted() -> None:
    """I2. The audio is the legal artefact; a transcript that disagrees with it is worse than one that shows the correction."""
    utterances = [_utt(0, "left kidney", 0), _utt(1, "I mean right kidney", 12)]
    apply_repairs(utterances, detect_repairs(utterances))

    assert len(utterances) == 2
    assert utterances[0].text == "left kidney"


def test_a_within_utterance_repair_retracts_only_the_part_before_the_cue() -> None:
    utterances = [_utt(0, "the left kidney, sorry, the right kidney is enlarged", 0)]
    repairs = detect_repairs(utterances)

    assert repairs[0].retracted_text == "the left kidney,"
    assert repairs[0].correcting_text == "the right kidney is enlarged"


def test_ordinary_speech_is_not_retracted() -> None:
    """A cue list that fires on "actually" silently deletes real findings."""
    utterances = [_utt(0, "the liver is actually rather large", 0), _utt(1, "the spleen is normal", 40)]
    assert detect_repairs(utterances) == []


def test_a_repair_that_changes_nothing_clinical_is_not_flagged_high_stakes() -> None:
    utterances = [_utt(0, "the kidney measures", 0), _utt(1, "sorry, the kidney is normal", 20)]
    repairs = detect_repairs(utterances)
    assert repairs and repairs[0].changes_high_stakes_term is False


# ===================================================== critical alerts ==
def _rule(code: str, patterns: tuple[str, ...], severity: str = AlertSeverity.RED):
    return CriticalRuleEntry(rule_id=uuid.uuid4(), rule_code=code, finding_label=code.title(), pattern_type=PatternType.LEXICAL, patterns=patterns, negation_sensitive=True, severity=severity, sla_minutes=30)


RULES = (_rule("PNEUMOTHORAX", ("pneumothorax",)),)


def test_a_negated_finding_does_not_alert() -> None:
    assert detect_alerts("There is no pneumothorax.", RULES) == []


def test_a_real_finding_alerts_even_beside_another_negation() -> None:
    """A document-wide negation scan suppresses this — it must be sentence-local and, within a sentence, positional."""
    alerts = detect_alerts("No fracture is seen. A large pneumothorax is present.", RULES)
    assert len(alerts) == 1
    assert alerts[0].rule_code == "PNEUMOTHORAX"


def test_a_negation_after_the_finding_does_not_suppress_it() -> None:
    """ "Large pneumothorax; no effusion" is an alert, not a negation."""
    alerts = detect_alerts("Large pneumothorax, no pleural effusion.", RULES)
    assert len(alerts) == 1


def test_a_hedge_lowers_confidence_but_never_suppresses() -> None:
    """ "Possible pneumothorax" is exactly the call worth making (: recall at any precision cost)."""
    alerts = detect_alerts("Possible pneumothorax in the left apex.", RULES)
    assert len(alerts) == 1
    assert alerts[0].hedged is True
    assert alerts[0].confidence < 0.95


def test_a_retracted_span_does_not_alert() -> None:
    """ "pneumothorax, sorry, no pneumothorax" must not trigger a phone call."""
    utterances = [_utt(0, "there is a pneumothorax", 0, superseded_by_seq=1, is_included_downstream=False), _utt(1, "sorry, no pneumothorax", 24)]
    alerts = detect_alerts("ignored", RULES, utterances=utterances)
    assert alerts == []


def test_an_aside_is_still_scanned() -> None:
    """A critical finding said to a colleague in the room is still a critical finding. This asymmetry with retractions is deliberate."""
    utterances = [_utt(0, "look at this pneumothorax", 0, label=UtteranceLabel.ASIDE)]
    alerts = detect_alerts("ignored", RULES, utterances=utterances)
    assert len(alerts) == 1


def test_red_alerts_sort_before_orange() -> None:
    rules = (_rule("BOWEL_OBSTRUCTION", ("bowel obstruction",), AlertSeverity.ORANGE), _rule("PNEUMOTHORAX", ("pneumothorax",), AlertSeverity.RED))
    alerts = detect_alerts("Bowel obstruction noted. Large pneumothorax.", rules)
    assert [a.severity for a in alerts] == [AlertSeverity.RED, AlertSeverity.ORANGE]
    assert highest_severity(alerts) == AlertSeverity.RED


# ================================================== I1 grounding / coverage ==
TRANSCRIPT = "the left kidney contains a simple cyst measuring 3.2 cm"


def _grounded_value(quote: str, *, seq: int = 0, **kwargs) -> FieldValue:
    start = TRANSCRIPT.index(quote)
    return FieldValue(field_key="kidney", value_text=quote, confidence=0.9, provenance=[ProvenanceRef(utterance_seq=seq, char_start=start, char_end=start + len(quote), audio_start_ms=0, audio_end_ms=1000, quote=quote)], **kwargs)


def test_a_paraphrased_quote_fails_grounding() -> None:
    """The model must cite what the transcript says, not what it meant."""
    state = _state(TRANSCRIPT)
    value = _grounded_value("simple cyst")
    value.provenance[0].quote = "a simple renal cyst"
    state.field_values = {"kidney": value}

    report = ground_field_values(state)
    assert report.ungrounded == 1
    assert "verbatim" in report.failures[0].reason
    assert value.is_grounded is False
    assert renderable(state) == {}


def test_a_quote_that_appears_elsewhere_does_not_ground_the_cited_span() -> None:
    """Searching the whole transcript would pass this."""
    ref = ProvenanceRef(char_start=0, char_end=8, audio_start_ms=0, audio_end_ms=10, quote="left kidney")
    assert quote_is_verbatim(TRANSCRIPT, ref) is False


def test_a_quote_from_a_retracted_utterance_cannot_ground_a_field() -> None:
    """This is what turns the repair stage into an actual safety control."""
    state = _state(TRANSCRIPT)
    state.utterances = [_utt(0, TRANSCRIPT, 0, superseded_by_seq=1, is_included_downstream=False)]
    state.field_values = {"kidney": _grounded_value("left kidney", seq=0)}

    report = ground_field_values(state)
    assert report.ungrounded == 1
    assert "excluded downstream" in report.failures[0].reason


def test_a_value_with_no_provenance_fails() -> None:
    """I1 admits no exception for a value the model was confident about."""
    state = _state(TRANSCRIPT)
    state.field_values = {"kidney": FieldValue(field_key="kidney", value_text="cyst")}

    report = ground_field_values(state)
    assert report.ungrounded == 1
    assert "no provenance" in report.failures[0].reason


def test_a_template_default_reaching_grounding_is_itself_the_finding() -> None:
    """V1 auto-fills nothing. A default arriving here is a regression."""
    state = _state(TRANSCRIPT)
    state.field_values = {"liver": FieldValue(field_key="liver", value_text="Unremarkable", fill_source=FillSource.TEMPLATE_DEFAULT)}
    report = ground_field_values(state)
    assert "auto-fills nothing" in report.failures[0].reason


def test_orphan_assertions_are_counted_as_the_wrong_template_signal() -> None:
    """Findings the chosen template has nowhere to put mean the template is probably wrong."""
    state = _state(TRANSCRIPT)
    state.utterances = [_utt(0, TRANSCRIPT, 0)]
    state.field_values = {"kidney": _grounded_value("simple cyst", seq=0)}
    state.sketch = FindingSketch(assertions=["simple cyst", "splenomegaly"])

    report = ground_field_values(state)
    assert report.orphan_assertions == ["splenomegaly"]


def test_a_good_value_grounds_and_renders() -> None:
    state = _state(TRANSCRIPT)
    state.utterances = [_utt(0, TRANSCRIPT, 0)]
    state.field_values = {"kidney": _grounded_value("simple cyst", seq=0)}

    report = ground_field_values(state)
    assert (report.grounded, report.ungrounded) == (1, 0)
    assert set(renderable(state)) == {"kidney"}


# ====================================================== verification ==
def test_laterality_disagreeing_with_its_own_quote_blocks() -> None:
    state = _state(TRANSCRIPT)
    value = _grounded_value("left kidney")
    value.laterality = Laterality.RIGHT
    state.field_values = {"kidney": value}

    findings = run_checks(state)
    assert any(f.check_id == "laterality_disagrees_with_source" and f.severity == Severity.BLOCK for f in findings)


def test_a_present_assertion_from_a_negated_quote_blocks() -> None:
    text = "there is no pleural effusion"
    state = _state(text)
    state.field_values = {"effusion": FieldValue(field_key="effusion", assertion_status=AssertionStatus.PRESENT, provenance=[ProvenanceRef(char_start=0, char_end=len(text), audio_start_ms=0, audio_end_ms=10, quote=text)])}
    findings = run_checks(state)
    assert any(f.check_id == "assertion_contradicts_source" for f in findings)


def test_a_number_not_present_in_its_quote_is_an_error() -> None:
    """A transposed digit survives every plausibility bound."""
    state = _state(TRANSCRIPT)
    value = _grounded_value("measuring 3.2 cm")
    value.value_numeric = 2.3
    value.value_unit = "cm"
    state.field_values = {"kidney": value}

    findings = run_checks(state)
    assert any(f.check_id == "measurement_not_in_source" for f in findings)


def test_an_implausible_measurement_warns_rather_than_blocks() -> None:
    """The check cannot tell an outlier from a misheard decimal, and blocking would train reviewers to dismiss it."""
    state = _state(TRANSCRIPT)
    value = _grounded_value("measuring 3.2 cm")
    value.value_numeric = 320.0
    value.value_unit = "cm"
    state.field_values = {"kidney": value}

    findings = [f for f in run_checks(state) if f.check_id == "measurement_out_of_range"]
    assert findings and findings[0].severity == Severity.WARN


def test_any_auto_fill_blocks_in_v1() -> None:
    """Any auto-filled value blocks the draft, checked rather than trusted: "by construction" is a property of code someone will edit."""
    state = _state(TRANSCRIPT)
    state.field_values = {"liver": FieldValue(field_key="liver", value_text="Normal", fill_source=FillSource.BLANKET_NORMAL)}
    findings = run_checks(state)
    assert any(f.check_id == "auto_fill_in_v1" and f.severity == Severity.BLOCK for f in findings)


# ======================================================= confidence ===
def test_one_weak_critical_field_dominates_thirty_strong_ones() -> None:
    """The case averaging hides. 30×0.95 + 1×0.2 averages to 0.93."""
    state = _state(TRANSCRIPT)
    state.field_values = {f"f{i}": FieldValue(field_key=f"f{i}", confidence=0.95, is_grounded=True) for i in range(30)}
    state.field_values["critical"] = FieldValue(field_key="critical", confidence=0.2, is_grounded=True)

    breakdown = compute_confidence(state, critical_field_keys=frozenset({"critical"}))
    assert breakdown.all_mean > 0.9
    assert breakdown.critical_min == 0.2
    assert breakdown.overall < 0.2
    assert breakdown.weakest_field == "critical"
    assert needs_radiologist(breakdown) is True


def test_an_ungrounded_field_contributes_zero_rather_than_being_skipped() -> None:
    """Skipping lets a report whose extraction mostly failed score on the few fields that survived."""
    state = _state(TRANSCRIPT)
    state.field_values = {"a": FieldValue(field_key="a", confidence=0.95, is_grounded=True), "b": FieldValue(field_key="b", confidence=0.95, is_grounded=False)}
    breakdown = compute_confidence(state)
    assert breakdown.all_mean == pytest.approx(0.475)
    assert breakdown.ungrounded_count == 1


def test_a_blocking_finding_forces_confidence_to_zero() -> None:
    """A draft that contradicts itself has no meaningful confidence."""
    state = _state(TRANSCRIPT)
    state.field_values = {"a": FieldValue(field_key="a", confidence=0.99, is_grounded=True)}
    state.verification = [VerificationFindingState(check_id="laterality_disagrees_with_source", check_type=CheckType.RULE, severity=Severity.BLOCK, message="contradiction")]
    breakdown = compute_confidence(state)
    assert breakdown.overall == 0.0
    assert breakdown.blocked is True
    assert needs_radiologist(breakdown) is True


def test_no_critical_fields_means_the_safeguard_is_inactive_and_says_so() -> None:
    state = _state(TRANSCRIPT)
    state.field_values = {"a": FieldValue(field_key="a", confidence=0.9, is_grounded=True)}
    breakdown = compute_confidence(state)

    assert breakdown.critical_min is None
    assert breakdown.overall == breakdown.all_mean


def test_a_critical_alert_routes_to_a_radiologist_whatever_the_confidence() -> None:
    """A high-confidence draft describing a pneumothorax is the most important reason to send it to a radiologist, not a reason not to."""
    state = _state(TRANSCRIPT)
    state.field_values = {"a": FieldValue(field_key="a", confidence=1.0, is_grounded=True)}
    breakdown = compute_confidence(state)

    assert breakdown.overall == 1.0
    assert needs_radiologist(breakdown, has_critical_alert=False) is False
    assert needs_radiologist(breakdown, has_critical_alert=True) is True


def test_an_enum_value_outside_the_schema_is_caught() -> None:
    """Constrained decoding should make this impossible, which is why it is checked: a silent fallback to unconstrained generation shows here first."""
    state = _state(TRANSCRIPT)
    state.field_values = {"pleura": FieldValue(field_key="pleura", value_enum="perhaps", is_grounded=True)}
    options = {"pleura": ("present", "absent")}

    assert run_checks(state, enum_options=options), "an out-of-schema enum must be caught"
    finding = next(f for f in run_checks(state, enum_options=options) if f.check_id == "enum_value_out_of_schema")
    assert finding.severity == Severity.ERROR
    assert finding.evidence["allowed"] == ["present", "absent"]

    # And a valid value passes.
    state.field_values["pleura"].value_enum = "absent"
    assert not [f for f in run_checks(state, enum_options=options) if f.check_id == "enum_value_out_of_schema"]


def test_a_within_utterance_repair_splits_rather_than_excluding_the_whole_span() -> None:
    """The correction must survive the retraction."""
    text = "there is a 3.2 cm simple cyst in the left kidney, sorry, the right kidney"
    utterances = [_utt(0, text, 0)]
    apply_repairs(utterances, detect_repairs(utterances))

    assert len(utterances) == 2, "the utterance must be split at the cue"
    retracted, correcting = utterances

    assert retracted.is_included_downstream is False
    assert "left kidney" in retracted.text
    assert retracted.superseded_by_seq == correcting.seq

    assert correcting.is_included_downstream is True
    assert "right kidney" in correcting.text
    assert correcting.label == UtteranceLabel.REPORT_CONTENT

    # Offsets still resolve against the original transcript (I1).
    for utterance in utterances:
        assert text[utterance.char_start : utterance.char_end] == utterance.text

    # Sequence numbers stay dense and ordered after the split.
    assert [u.seq for u in utterances] == [0, 1]


def test_a_cross_utterance_repair_still_supersedes_the_whole_utterance() -> None:
    """The split path must not break the case it was added alongside."""
    utterances = [_utt(0, "cyst in the left kidney", 0), _utt(1, "sorry, the right kidney", 24)]
    apply_repairs(utterances, detect_repairs(utterances))

    assert len(utterances) == 2
    assert utterances[0].is_included_downstream is False
    assert utterances[0].superseded_by_seq == 1
    assert utterances[1].is_included_downstream is True
