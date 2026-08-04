"""Splitting clinical prose into sentences."""

from __future__ import annotations

import uuid

import pytest

from radreport.core.text import iter_sentences_with_offsets, split_sentences
from radreport.core.types import AlertSeverity, PatternType
from radreport.pipeline.stages.critical import detect_alerts
from radreport.pipeline.stages.providers import CriticalRuleEntry


@pytest.mark.parametrize(("text", "expected"), [("a 3.2 cm cyst. no stone.", ["a 3.2 cm cyst", "no stone"]), ("measures 1.5 mm", ["measures 1.5 mm"]), ("liver normal; spleen normal", ["liver normal", "spleen normal"]), ("one\ntwo\n\nthree", ["one", "two", "three"]), ("Is there a mass? No.", ["Is there a mass", "No."]), ("", []), ("   ", [])])
def test_decimals_survive_and_real_boundaries_still_split(text, expected) -> None:
    assert split_sentences(text) == expected


def test_a_trailing_abbreviation_does_not_end_a_sentence() -> None:
    """An over-long sentence is a far cheaper error than a split measurement."""
    assert split_sentences("Reviewed by Dr. Anand today") == ["Reviewed by Dr. Anand today"]


def test_offsets_point_at_the_sentence_they_name() -> None:
    """Any stage reporting a sentence as evidence needs its position, because provenance cites character ranges."""
    text = "a 3.2 cm cyst. no stone."
    for start, end, sentence in iter_sentences_with_offsets(text):
        assert text[start:end] == sentence


_RULE = CriticalRuleEntry(rule_id=uuid.uuid4(), rule_code="PNEUMOTHORAX", finding_label="Pneumothorax", pattern_type=PatternType.LEXICAL, patterns=("pneumothorax",), negation_sensitive=True, severity=AlertSeverity.RED, sla_minutes=30)


def test_a_negated_finding_with_a_measurement_does_not_alert() -> None:
    """The regression."""
    assert detect_alerts("There is no 3.2 cm pneumothorax identified.", (_RULE,)) == []


def test_a_real_finding_with_a_measurement_still_alerts() -> None:
    """The fix must not buy quiet by suppressing real findings."""
    alerts = detect_alerts("A 3.2 cm pneumothorax is present.", (_RULE,))
    assert len(alerts) == 1


def test_measurements_survive_into_the_finding_sketch() -> None:
    from radreport.pipeline.stages.sketch import fallback_sketch

    sketch = fallback_sketch("the cyst measures 3.2 cm. no hydronephrosis.")
    assert any(m["value"] == 3.2 and m["unit"] == "cm" for m in sketch.measurements)
    assert "measures 3.2 cm" in " ".join(sketch.assertions)
