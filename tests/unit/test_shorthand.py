"""Shorthand reference sheets in the formats hospitals actually hand over, read into short -> formal pairs."""

from __future__ import annotations

import pytest

from radreport.onboarding.shorthand import UnreadableReference, acronym_fit, extract_text, find_mappings
from tests.fixtures.pdf import text_pdf

#: Five real-world layouts: an "=" list, an arrow list, a markdown table, a colon-and-dash mix, and a two-column sheet.
FORMATS: dict[str, tuple[str, bytes]] = {"equals.txt": ("equals", b"ABBREVIATIONS\nLLL = Left lower lobe\nRLL = Right lower lobe\nPNA = Pneumonia\nCBD = Common bile duct\n"), "arrows.txt": ("arrow", "Transcription shortcuts\nRLL → Right lower lobe\nIVC -> Inferior vena cava\nGGO => ground glass opacity\n".encode()), "table.md": ("pipe", b"| Short | Meaning |\n|---|---|\n| PNA | Pneumonia |\n| HSM | Hepatosplenomegaly |\n| CXR | Chest X-ray |\n"), "mixed.txt": ("colon", b"CBD: common bile duct\nIVC - inferior vena cava\nSOL: space occupying lesion\nNormal: none\n"), "columns.txt": ("columns", b"USG      Ultrasonography\nCECT     Contrast enhanced CT\nHRCT     High resolution CT\n")}


@pytest.mark.parametrize("filename", sorted(FORMATS))
def test_each_hospital_format_yields_its_pairs(filename: str) -> None:
    pattern, data = FORMATS[filename]
    mappings = find_mappings(extract_text(data, filename))
    assert len(mappings) >= 3, mappings
    assert pattern in {m.pattern for m in mappings}
    assert all(m.confidence >= 0.5 for m in mappings)


def test_a_pdf_reference_is_read() -> None:
    pdf = text_pdf(["Radiology department shorthand", "LLL = Left lower lobe", "PNA = Pneumonia", "RUL/LUL/RLL/LLL"])
    mappings = {m.short: m for m in find_mappings(extract_text(pdf, "reference.pdf"))}
    assert mappings["LLL"].formal == "Left lower lobe" and mappings["PNA"].formal == "Pneumonia"
    # The slash group adds the members the sheet did not spell out, from the lobe convention.
    assert mappings["RUL"].formal == "right upper lobe" and mappings["RUL"].pattern == "slash_group"


def test_headings_and_noise_are_not_pairs() -> None:
    lines = ["FINDINGS:", "IMPRESSION", "Normal: none", "Page 3 of 4", "XYZ = 42", "Dr: Rao"]
    assert find_mappings(lines) == []


def test_a_layout_guess_needs_the_letters_to_line_up() -> None:
    assert find_mappings(["CBD      Gall bladder wall thickness"]) == []
    assert find_mappings(["CBD      Common bile duct"])[0].formal == "Common bile duct"


def test_acronym_fit() -> None:
    assert acronym_fit("LLL", "left lower lobe") == 1.0
    assert acronym_fit("HRCT", "high resolution CT") == 1.0
    assert acronym_fit("PNA", "pneumonia") >= 0.75
    assert acronym_fit("CBD", "gall bladder") < 0.5


def test_an_unknown_format_is_refused() -> None:
    with pytest.raises(UnreadableReference):
        extract_text(b"\x00\x01", "reference.xlsx")
    with pytest.raises(UnreadableReference):
        extract_text(b"not a pdf", "reference.pdf")
