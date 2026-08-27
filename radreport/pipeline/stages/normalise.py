"""Stage 3: decides which of the lab's terms a misheard phrase meant, and refuses to guess when two are too close to call.

Order: find the candidate terms for each span (resolve_spans, phonetic_key) -> pick a winner, or
escalate when the margin is too small (escalations) -> record what was used (glossary,
spelled_form).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from radreport.core.logging import get_logger
from radreport.knowledge.phonetics import TAU_MARGIN, acronym_distance, double_metaphone, is_spelled_acronym, normalised_levenshtein, strip_non_alpha
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.stages.providers import KnowledgeProvider, LexiconEntry
from radreport.pipeline.state import PipelineState, TermResolution

log = get_logger(__name__)

#: Spelled letters arrive from ASR spaced ("L M P"), joined ("LMP"), or
#: dotted ("L.M.P."). All three are the same utterance.
_SPELLED_RUN = re.compile(r"\b(?:[A-Za-z][\.\s]{0,2}){2,5}\b")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'-]*")
#: Windows to test against multi-word terms: "left main coronary" is one term
#: said as three words, and no unigram scan will ever see it.
_MAX_NGRAM = 3


@dataclass(frozen=True, slots=True)
class _Candidate:
    canonical_form: str
    surface: str
    is_ambiguous: bool
    #: Precomputed once per run.
    letters: str = ""
    is_acronym: bool = False
    key: str = ""
    alt_key: str | None = None


class NormaliseStage:
    """Pure function of (transcript, lexicon)."""

    name = "normalise"
    version = "1.0.0"

    def __init__(self, knowledge: KnowledgeProvider, *, tau_margin: float = TAU_MARGIN, max_distance: float = 0.12) -> None:
        self._knowledge = knowledge
        self._tau = tau_margin
        #: A surface farther than this from every term is simply not that term.
        self._max_distance = max_distance

    def is_idempotent(self) -> bool:
        return True

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if state.transcript is None:
            raise ValueError("normalise runs on a transcript; none is present")

        knowledge = self._knowledge.for_tenant(state.tenant_id)
        entries = knowledge.code_words()
        blocked = _blocked_labels(knowledge.unresolved_blocking_collisions)

        resolutions = resolve_spans(state.transcript.text, entries, tau_margin=self._tau, max_distance=self._max_distance, blocked_labels=blocked)
        state.resolutions = resolutions

        escalated = [r for r in resolutions if r.escalated]
        warnings = [f"{r.surface!r} at {r.char_start}: too close to call between {', '.join(r.alternatives)} (margin {r.margin:.3f} < {self._tau})" for r in escalated]

        log.info("normalise_complete", resolutions=len(resolutions), escalated=len(escalated), blocked_pairs=len(blocked), tenant_id=str(state.tenant_id))
        return StageResult(
            output=state,
            # A run with an escalated span is not low-confidence overall; it has one span a human must look at.
            confidence=1.0,
            warnings=warnings,
        )


def _blocked_labels(pairs: tuple[tuple[str, str], ...]) -> frozenset[str]:
    return frozenset(label.upper() for pair in pairs for label in pair)


def resolve_spans(text: str, entries: tuple[LexiconEntry, ...], *, tau_margin: float = TAU_MARGIN, max_distance: float = 0.12, blocked_labels: frozenset[str] = frozenset()) -> list[TermResolution]:
    """Find resolvable spans and apply the margin guard to each."""
    if not entries:
        return []

    candidates = _candidate_index(entries)
    resolutions: list[TermResolution] = []
    #: Spans arrive in increasing start order, so every claim already made starts at or before the current span.
    max_claimed_end = -1

    for start, end, surface in _spans(text):
        if start < max_claimed_end:
            continue

        probe = _probe(surface)
        scored = sorted(((candidate, distance) for candidate, distance in _score(probe, candidates, max_distance)), key=lambda pair: (pair[1], pair[0].canonical_form))
        if not scored:
            continue
        best, best_distance = scored[0]
        if best_distance > max_distance:
            continue
        if surface.upper() == best.canonical_form.upper():
            # The text already says the canonical form *literally* — nothing to resolve.
            continue

        # The runner-up must be a **different term**.
        second_distance = next((d for c, d in scored if c.canonical_form != best.canonical_form), 1.0)
        margin = round(second_distance - best_distance, 4)
        alternatives = sorted({c.canonical_form for c, d in scored if d <= max_distance})[:2]

        is_blocked = best.canonical_form.upper() in blocked_labels
        escalated = margin < tau_margin or is_blocked

        resolutions.append(TermResolution(char_start=start, char_end=end, surface=surface, canonical_form=None if escalated else best.canonical_form, margin=margin, escalated=escalated, alternatives=alternatives if escalated else [], is_ambiguous_term=best.is_ambiguous))
        max_claimed_end = max(max_claimed_end, end)

    return resolutions


def _candidate_index(entries: tuple[LexiconEntry, ...]) -> list[_Candidate]:
    """One entry per (term, surface) pair the resolver may match against."""
    candidates: list[_Candidate] = []
    for entry in entries:
        surfaces = {entry.canonical_form}
        if entry.short_form:
            surfaces.add(entry.short_form)
        surfaces.update(entry.surface_variants)
        for surface in sorted(surfaces):
            probe = _probe(surface)
            candidates.append(_Candidate(canonical_form=entry.canonical_form, surface=surface, is_ambiguous=entry.is_ambiguous, letters=probe.letters, is_acronym=probe.is_acronym, key=probe.key, alt_key=probe.alt_key))
    return candidates


@dataclass(frozen=True, slots=True)
class _Probe:
    """A span or candidate surface, with everything distance needs precomputed."""

    surface: str
    letters: str
    is_acronym: bool
    key: str
    alt_key: str | None


def _probe(surface: str) -> _Probe:
    key, alt = double_metaphone(surface)
    return _Probe(surface=surface, letters=strip_non_alpha(surface), is_acronym=is_spelled_acronym(surface), key=key, alt_key=alt)


def _score(probe: _Probe, candidates: list[_Candidate], max_distance: float) -> list[tuple[_Candidate, float]]:
    """Distances for the candidates that could possibly be within range."""
    scored: list[tuple[_Candidate, float]] = []
    probe_key_len = len(probe.key)

    for candidate in candidates:
        if probe.is_acronym and candidate.is_acronym:
            if len(probe.letters) != len(candidate.letters):
                continue
            scored.append((candidate, acronym_distance(probe.surface, candidate.surface)))
            continue

        if probe.key and candidate.key:
            longest = max(probe_key_len, len(candidate.key))
            if longest and abs(probe_key_len - len(candidate.key)) / longest > max_distance:
                continue

        scored.append((candidate, _metaphone_distance(probe, candidate)))

    return scored


def _metaphone_distance(probe: _Probe, candidate: _Candidate) -> float:
    """`phonetic_distance`'s non-acronym branch, over precomputed keys."""
    if probe.key and probe.key == candidate.key:
        return 0.0
    if probe.alt_key and probe.alt_key in {candidate.key, candidate.alt_key}:
        return 0.1
    if candidate.alt_key and candidate.alt_key == probe.key:
        return 0.1
    return normalised_levenshtein(probe.key, candidate.key)


def _spans(text: str) -> list[tuple[int, int, str]]:
    """Candidate spans: spelled-letter runs first, then word n-grams."""
    spans: list[tuple[int, int, str]] = []
    for match in _SPELLED_RUN.finditer(text):
        surface = match.group(0).strip()
        letters = re.sub(r"[^A-Za-z]", "", surface)
        if 2 <= len(letters) <= 5:
            spans.append((match.start(), match.start() + len(surface), surface))

    tokens = [(m.start(), m.end(), m.group(0)) for m in _TOKEN.finditer(text)]
    for size in range(1, _MAX_NGRAM + 1):
        for i in range(len(tokens) - size + 1):
            window = tokens[i : i + size]
            start, end = window[0][0], window[-1][1]
            spans.append((start, end, text[start:end]))

    # Longest first at the same start, so a 3-gram claims its span before the
    # unigram inside it can.
    return sorted(spans, key=lambda s: (s[0], -(s[1] - s[0])))


def glossary(resolutions: list[TermResolution]) -> dict[str, str]:
    """Resolved terms, for the extraction prompt's **stable** region."""
    return {r.surface: r.canonical_form for r in resolutions if r.canonical_form and not r.escalated}


def escalations(resolutions: list[TermResolution]) -> list[TermResolution]:
    """Spans a human must adjudicate. The review UI's flag list."""
    return [r for r in resolutions if r.escalated]


def spelled_form(text: str) -> str:
    """ "L.M.P." / "L M P" → "LMP". Used by study-code matching too."""
    return re.sub(r"[^A-Za-z]", "", text).upper()


def phonetic_key(text: str) -> str:
    return double_metaphone(spelled_form(text))[0]
