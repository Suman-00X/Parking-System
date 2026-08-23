"""Catches terms that sound alike, so the system never silently swaps one for another -- the hazard behind confusing LMC with LMP.

Order: reduce a term to how it sounds (double_metaphone, strip_non_alpha, phonetic_key helpers)
-> measure how close two terms are, spelled-out letters included (is_spelled_acronym,
confusable_letters, acronym_distance, phonetic_distance, normalised_levenshtein) -> audit the
whole vocabulary for collisions (audit_collisions) -> resolve one only when the winner is clear
(resolve_with_margin_guard).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from radreport.core.types import CollisionClass, CollisionSeverity

#: The confusable set. B/C/D/E/G/P/T/V/Z share the /iː/ rhyme.
E_SET: frozenset[str] = frozenset("BCDEGPTVZ")

#: Other letter confusions worth auditing: the /ɛ/ group and the nasals.
_OTHER_CONFUSABLE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset("FLMNSX"),  # ef / el / em / en / es / ex
    frozenset("AJK"),  # ay / jay / kay
    frozenset("IY"),  # eye / wy
    frozenset("QU"),  # cue / you
)

#: Below this margin the resolver escalates rather than picking.
TAU_MARGIN = 0.15

_NON_ALPHA = re.compile(r"[^A-Za-z]+")


@lru_cache(maxsize=16_384)
def double_metaphone(text: str) -> tuple[str, str | None]:
    """(primary, secondary) keys for `lexicon_term.phonetic_key_*`."""
    cleaned = _NON_ALPHA.sub("", text).upper()
    if not cleaned:
        return "", None

    try:
        from metaphone import doublemetaphone

        primary, secondary = doublemetaphone(cleaned)
        return primary or _fallback_key(cleaned), (secondary or None)
    except ImportError:  # pragma: no cover - exercised only without the dep
        return _fallback_key(cleaned), None


def _fallback_key(text: str) -> str:
    """Vowel-stripped consonant skeleton, first letter retained."""
    if not text:
        return ""
    head, tail = text[0], text[1:]
    skeleton = "".join(c for c in tail if c not in "AEIOU")
    collapsed: list[str] = []
    for char in head + skeleton:
        if not collapsed or collapsed[-1] != char:
            collapsed.append(char)
    return "".join(collapsed)[:8]


def is_spelled_acronym(text: str) -> bool:
    """Is this said letter-by-letter? 2–5 uppercase letters, no vowel run."""
    cleaned = _NON_ALPHA.sub("", text)
    return 2 <= len(cleaned) <= 5 and cleaned.isupper()


def confusable_letters(a: str, b: str) -> bool:
    """Do two letters belong to the same confusable rhyme group?"""
    if a == b:
        return True
    if a in E_SET and b in E_SET:
        return True
    return any(a in group and b in group for group in _OTHER_CONFUSABLE_GROUPS)


#: Cost of one confusable-letter substitution. Sized so a single one lands
#: below `TAU_MARGIN` and two land above it.
CONFUSABLE_SUBSTITUTION_COST = 0.12


def acronym_distance(a: str, b: str) -> float:
    """Distance in [0, 1] between two spelled acronyms."""
    left = _NON_ALPHA.sub("", a).upper()
    right = _NON_ALPHA.sub("", b).upper()
    if left == right:
        return 0.0
    if len(left) != len(right):
        return 1.0

    cost = 0.0
    for x, y in zip(left, right, strict=True):
        if x == y:
            continue
        cost += CONFUSABLE_SUBSTITUTION_COST if confusable_letters(x, y) else 1.0

    return min(1.0, cost)


def phonetic_distance(a: str, b: str) -> float:
    """General distance for `collision_audit_finding.phonetic_distance`."""
    if is_spelled_acronym(a) and is_spelled_acronym(b):
        return acronym_distance(a, b)

    key_a, alt_a = double_metaphone(a)
    key_b, alt_b = double_metaphone(b)
    if key_a and key_a == key_b:
        return 0.0
    if alt_a and alt_a in {key_b, alt_b}:
        return 0.1
    if alt_b and alt_b == key_a:
        return 0.1
    return _normalised_levenshtein(key_a, key_b)


def strip_non_alpha(text: str) -> str:
    """Letters only, upper-cased — the form both distance branches compare."""
    return _NON_ALPHA.sub("", text).upper()


def normalised_levenshtein(a: str, b: str) -> float:
    """Edit distance over the longer string."""
    return _normalised_levenshtein(a, b)


def _normalised_levenshtein(a: str, b: str) -> float:
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1] / max(len(a), len(b))


# --------------------------------------------------------------- audit -----
@dataclass(frozen=True, slots=True)
class CollisionCandidate:
    """One term in the audit. `maps_to` is what decides severity."""

    label: str
    maps_to: str | None = None
    """Template code, or a canonical clinical meaning."""

    term_id: str | None = None


@dataclass(frozen=True, slots=True)
class CollisionFinding:
    a: CollisionCandidate
    b: CollisionCandidate
    distance: float
    collision_class: str
    severity: str
    rationale: str


def audit_collisions(candidates: list[CollisionCandidate], *, threshold: float = TAU_MARGIN) -> list[CollisionFinding]:
    """Pairwise audit over code words and spoken study codes."""
    findings: list[CollisionFinding] = []

    for i, a in enumerate(candidates):
        for b in candidates[i + 1 :]:
            distance = phonetic_distance(a.label, b.label)
            if distance > threshold:
                continue

            collision_class = _classify(a.label, b.label, distance)
            # The severity rule, stated as code: `block` if both map
            # to different templates or opposite clinical meanings.
            different_targets = a.maps_to is not None and b.maps_to is not None and a.maps_to != b.maps_to
            severity = CollisionSeverity.BLOCK if different_targets else CollisionSeverity.WARN
            rationale = f"{a.label!r} and {b.label!r} are phonetically close (distance {distance:.2f} <= {threshold})"
            if different_targets:
                rationale += f" and map to different targets ({a.maps_to} vs {b.maps_to})"

            findings.append(CollisionFinding(a=a, b=b, distance=round(distance, 4), collision_class=collision_class, severity=severity, rationale=rationale))

    return sorted(findings, key=lambda f: (f.severity != CollisionSeverity.BLOCK, f.distance))


def _classify(a: str, b: str, distance: float) -> str:
    if is_spelled_acronym(a) and is_spelled_acronym(b):
        left = _NON_ALPHA.sub("", a).upper()
        right = _NON_ALPHA.sub("", b).upper()
        if len(left) == len(right):
            diffs = [(x, y) for x, y in zip(left, right, strict=True) if x != y]
            if len(diffs) == 1 and confusable_letters(*diffs[0]):
                return CollisionClass.E_SET_LETTER
    if distance == 0.0:
        return CollisionClass.HOMOPHONE
    if distance <= 0.1:
        return CollisionClass.NEAR_HOMOPHONE
    return CollisionClass.NATURAL_WORD_OVERLAP


def resolve_with_margin_guard(query: str, candidates: list[CollisionCandidate], *, tau: float = TAU_MARGIN) -> tuple[CollisionCandidate | None, float, bool]:
    """The resolution step. Returns (best, margin, escalate)."""
    if not candidates:
        return None, 0.0, True

    scored = sorted(((c, phonetic_distance(query, c.label)) for c in candidates), key=lambda pair: pair[1])
    best, best_distance = scored[0]
    if len(scored) == 1:
        return best, 1.0, False

    _second, second_distance = scored[1]
    margin = second_distance - best_distance
    return best, round(margin, 4), margin < tau
