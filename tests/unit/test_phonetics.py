"""Property tests on the sound-alike index, covering the letters people confuse when spelling aloud -- the class of failure that confuses LMC with LMP."""

from __future__ import annotations

import itertools

import pytest

from radreport.core.types import CollisionClass, CollisionSeverity
from radreport.knowledge.phonetics import E_SET, TAU_MARGIN, CollisionCandidate, acronym_distance, audit_collisions, confusable_letters, double_metaphone, phonetic_distance, resolve_with_margin_guard


def test_double_metaphone_alone_would_miss_lmc_lmp() -> None:
    """The reason the distance function cannot just compare metaphone keys."""
    assert double_metaphone("LMC")[0] != double_metaphone("LMP")[0]


def test_lmc_lmp_are_flagged() -> None:
    assert phonetic_distance("LMC", "LMP") < TAU_MARGIN


@pytest.mark.parametrize("a,b", list(itertools.combinations("BCDEGPTVZ", 2)))
def test_all_e_set_pairs_are_confusable(a: str, b: str) -> None:
    """Property: every pair within the E-set rhymes, so every pair confuses."""
    assert confusable_letters(a, b)
    assert acronym_distance(f"LM{a}", f"LM{b}") < TAU_MARGIN


@pytest.mark.parametrize("letter", sorted(E_SET))
def test_e_set_does_not_confuse_with_non_rhyming_letters(letter: str) -> None:
    for other in "HLNORSW":
        if other in E_SET:
            continue
        assert not confusable_letters(letter, other)
        assert acronym_distance(f"LM{letter}", f"LM{other}") >= TAU_MARGIN


def test_short_acronyms_are_not_penalised_for_being_short() -> None:
    """A one-letter confusion is as dangerous in `PA`/`TA` as in a long code."""
    assert acronym_distance("PA", "TA") == acronym_distance("LMC", "LMP")
    assert acronym_distance("PA", "TA") < TAU_MARGIN


def test_two_confusable_substitutions_are_not_flagged() -> None:
    """Both letters being misheard at once is a far less likely event."""
    assert acronym_distance("PB", "TD") >= TAU_MARGIN


def test_different_lengths_never_collide() -> None:
    assert acronym_distance("LMC", "LMCP") == 1.0


def test_block_severity_requires_different_targets() -> None:
    """`block` if both map to different templates or opposite meanings."""
    different = audit_collisions([CollisionCandidate("LMC", "last menstrual cycle"), CollisionCandidate("LMP", "last menstrual period")])
    assert len(different) == 1
    assert different[0].severity == CollisionSeverity.BLOCK
    assert different[0].collision_class == CollisionClass.E_SET_LETTER

    same = audit_collisions([CollisionCandidate("LMC", "US_ABD"), CollisionCandidate("LMP", "US_ABD")])
    assert same[0].severity == CollisionSeverity.WARN


def test_audit_covers_spoken_study_codes_not_just_shorthand() -> None:
    """Runs the audit over both."""
    findings = audit_collisions([CollisionCandidate("ultrasound abdomen routine", "US_ABD_ROUTINE"), CollisionCandidate("ultrasound abdomen routine", "US_ABD_FULL")])
    assert findings and findings[0].severity == CollisionSeverity.BLOCK


def test_margin_guard_escalates_rather_than_guessing() -> None:
    """The margin guard — what stops LMC resolving to LMP silently."""
    candidates = [CollisionCandidate("LMC", "last menstrual cycle"), CollisionCandidate("LMP", "last menstrual period")]
    _best, _margin, escalate = resolve_with_margin_guard("LMB", candidates)
    assert escalate, "an ambiguous code word must escalate, not resolve"


def test_margin_guard_resolves_an_unambiguous_match() -> None:
    candidates = [CollisionCandidate("LMC", "last menstrual cycle"), CollisionCandidate("UGS", "ultrasound guided study")]
    best, margin, escalate = resolve_with_margin_guard("LMC", candidates)
    assert best is not None and best.label == "LMC"
    assert not escalate
    assert margin >= TAU_MARGIN


def test_audit_is_deterministic() -> None:
    """Golden-file discipline: the audit must be bit-reproducible."""
    candidates = [CollisionCandidate("LMC", "a"), CollisionCandidate("LMP", "b"), CollisionCandidate("PA", "c"), CollisionCandidate("TA", "d")]
    first = [(f.a.label, f.b.label, f.distance, f.severity) for f in audit_collisions(candidates)]
    second = [(f.a.label, f.b.label, f.distance, f.severity) for f in audit_collisions(candidates)]
    assert first == second
