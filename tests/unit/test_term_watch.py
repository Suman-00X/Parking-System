"""What counts as new vocabulary in an edit: only what was added, and never phrases edged by filler."""

from __future__ import annotations

from radreport.onboarding.term_watch import added_text, candidates


def test_only_the_added_words_are_considered() -> None:
    runs = added_text("Lungs are clear.", "Lungs show ground glass opacity in both lower lobes.")
    words = {w.lower() for run in runs for w in run}
    assert {"ground", "glass", "opacity"} <= words and "lungs" not in words


def test_candidates_are_phrases_acronyms_and_long_words_without_filler_edges() -> None:
    found = {c.surface for c in candidates("There is mosaic perfusion with GGO and bronchiolectasis noted".split())}
    assert "mosaic perfusion" in found and "GGO" in found and "bronchiolectasis" in found
    assert not any(c.startswith(("there", "is ", "with")) or c.endswith((" noted", " is", " with")) for c in found)
    assert "is" not in found and "noted" not in found


def test_numbers_and_short_words_are_not_terms() -> None:
    assert candidates("measures 3.2 cm today".split()) == []
