"""Combines several engines' transcripts into one by voting on each word, and flags the spans they could not agree on.

Order: pick the transcript to align against (choose_base, from_asr_results) -> align the rest
into a word network (build_network) -> vote (reconcile) -> send the still-disputed spans for
arbitration (arbitration_payload, apply_arbitration).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from radreport.adapters.asr.base import ASRResult, Word
from radreport.core.logging import get_logger
from radreport.eval.metrics.alignment import tokenize

log = get_logger(__name__)

#: How much of a candidate's score comes from how many engines said it, versus how confident those engines were.
AGREEMENT_WEIGHT = 0.7

#: Below this margin between the top two candidates, a slot is **disputed** and goes to arbitration rather than being resolved by the vote.
DISPUTE_MARGIN = 0.25

#: The NULL candidate — "this engine heard nothing here".
NULL = ""


@dataclass(frozen=True, slots=True)
class EngineHypothesis:
    """One engine's output, with the identity needed to weight and audit it."""

    engine: str
    engine_version: str
    text: str
    words: tuple[Word, ...] = ()
    weight: float = 1.0
    """Per-engine prior, from the bake-off."""

    def tokens(self) -> list[str]:
        return tokenize(self.text)

    def confidences(self) -> list[float]:
        """Per-token confidence, defaulting to neutral where absent."""
        by_token = [w.confidence if w.confidence is not None else 0.5 for w in self.words]
        tokens = self.tokens()
        if len(by_token) == len(tokens):
            return by_token
        return [0.5] * len(tokens)


@dataclass(slots=True)
class Slot:
    """One position in the word transition network."""

    index: int
    #: `{candidate_token: [(engine, weight, confidence), ...]}`. NULL is a key
    #: like any other — that is what makes deletions votable.
    votes: dict[str, list[tuple[str, float, float]]] = field(default_factory=dict)

    def add(self, token: str, engine: str, weight: float, confidence: float) -> None:
        self.votes.setdefault(token, []).append((engine, weight, confidence))

    def score(self, token: str, total_weight: float) -> float:
        entries = self.votes.get(token, [])
        if not entries or total_weight <= 0:
            return 0.0
        agreement = sum(w for _e, w, _c in entries) / total_weight
        confidence = sum(c for _e, _w, c in entries) / len(entries)
        return AGREEMENT_WEIGHT * agreement + (1 - AGREEMENT_WEIGHT) * confidence

    def ranked(self, total_weight: float) -> list[tuple[str, float]]:
        return sorted(((token, self.score(token, total_weight)) for token in self.votes), key=lambda pair: (-pair[1], pair[0]))


@dataclass(frozen=True, slots=True)
class DisputedSpan:
    """A slot the vote could not settle. The only thing arbitration sees."""

    slot_index: int
    candidates: tuple[tuple[str, float], ...]
    margin: float
    left_context: str
    right_context: str

    @property
    def is_deletion_dispute(self) -> bool:
        """The disagreement is about whether anything was said at all."""
        return any(token == NULL for token, _score in self.candidates[:2])


@dataclass(slots=True)
class RoverResult:
    tokens: list[str] = field(default_factory=list)
    text: str = ""
    disputed: list[DisputedSpan] = field(default_factory=list)
    slot_count: int = 0
    unanimous_slots: int = 0
    engines: tuple[str, ...] = ()

    @property
    def disagreement_score(self) -> float:
        """Share of slots where the engines did not all agree."""
        if not self.slot_count:
            return 0.0
        return round(1.0 - (self.unanimous_slots / self.slot_count), 4)

    @property
    def dispute_rate(self) -> float:
        """Share of slots needing arbitration — the cost of the LLM step."""
        if not self.slot_count:
            return 0.0
        return round(len(self.disputed) / self.slot_count, 4)


def _alignment_path(reference: list[str], hypothesis: list[str]) -> list[tuple[int | None, int | None]]:
    """Levenshtein backtrace as `(ref_index, hyp_index)` pairs."""
    ref_len, hyp_len = len(reference), len(hypothesis)
    costs = [[0] * (hyp_len + 1) for _ in range(ref_len + 1)]
    for i in range(ref_len + 1):
        costs[i][0] = i
    for j in range(hyp_len + 1):
        costs[0][j] = j
    for i in range(1, ref_len + 1):
        for j in range(1, hyp_len + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                costs[i][j] = costs[i - 1][j - 1]
            else:
                costs[i][j] = 1 + min(costs[i - 1][j - 1], costs[i - 1][j], costs[i][j - 1])

    path: list[tuple[int | None, int | None]] = []
    i, j = ref_len, hyp_len
    while i > 0 or j > 0:
        if i > 0 and j > 0 and reference[i - 1] == hypothesis[j - 1]:
            path.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i > 0 and j > 0 and costs[i][j] == costs[i - 1][j - 1] + 1:
            path.append((i - 1, j - 1))  # substitution
            i, j = i - 1, j - 1
        elif i > 0 and costs[i][j] == costs[i - 1][j] + 1:
            path.append((i - 1, None))  # deletion: hypothesis said nothing
            i -= 1
        else:
            path.append((None, j - 1))  # insertion: only the hypothesis said it
            j -= 1
    path.reverse()
    return path


def choose_base(hypotheses: list[EngineHypothesis]) -> int:
    """Index of the most *central* hypothesis, to align the others against."""
    if len(hypotheses) <= 2:
        return 0
    token_lists = [h.tokens() for h in hypotheses]
    best_index, best_cost = 0, float("inf")
    for i, tokens in enumerate(token_lists):
        cost = sum(len(_alignment_path(tokens, other)) for j, other in enumerate(token_lists) if i != j)
        if cost < best_cost:
            best_index, best_cost = i, cost
    return best_index


def build_network(hypotheses: list[EngineHypothesis]) -> list[Slot]:
    """Align every hypothesis onto the base and collect per-slot votes."""
    if not hypotheses:
        return []

    base_index = choose_base(hypotheses)
    base = hypotheses[base_index]
    base_tokens = base.tokens()
    base_confidences = base.confidences()

    #: `base position -> [tokens inserted just before it]`, per engine.
    slots: list[Slot] = [Slot(index=i) for i in range(len(base_tokens))]
    insertions: dict[int, list[Slot]] = defaultdict(list)

    for i, token in enumerate(base_tokens):
        slots[i].add(token, base.engine, base.weight, base_confidences[i])

    for hypothesis in hypotheses:
        if hypothesis is base:
            continue
        tokens = hypothesis.tokens()
        confidences = hypothesis.confidences()
        path = _alignment_path(base_tokens, tokens)

        seen_base: set[int] = set()
        insert_cursor = 0
        for ref_index, hyp_index in path:
            if ref_index is not None and hyp_index is not None:
                slots[ref_index].add(tokens[hyp_index], hypothesis.engine, hypothesis.weight, confidences[hyp_index])
                seen_base.add(ref_index)
                insert_cursor = ref_index + 1
            elif ref_index is not None:
                # This engine heard nothing where the base heard a word.
                slots[ref_index].add(NULL, hypothesis.engine, hypothesis.weight, 0.5)
                seen_base.add(ref_index)
                insert_cursor = ref_index + 1
            else:
                # Only this engine produced a token here — an insertion slot.
                bucket = insertions[insert_cursor]
                position = sum(1 for s in bucket if hypothesis.engine in _engines(s))
                while len(bucket) <= position:
                    bucket.append(Slot(index=-1))
                bucket[position].add(tokens[hyp_index], hypothesis.engine, hypothesis.weight, confidences[hyp_index])

    # Engines that skipped a base slot entirely still owe it a NULL vote.
    total_engines = {h.engine for h in hypotheses}
    for slot in slots:
        for engine in total_engines - _engines(slot):
            weight = next(h.weight for h in hypotheses if h.engine == engine)
            slot.add(NULL, engine, weight, 0.5)
    for bucket in insertions.values():
        for slot in bucket:
            for engine in total_engines - _engines(slot):
                weight = next(h.weight for h in hypotheses if h.engine == engine)
                slot.add(NULL, engine, weight, 0.5)

    ordered: list[Slot] = []
    for position in range(len(base_tokens) + 1):
        ordered.extend(insertions.get(position, []))
        if position < len(base_tokens):
            ordered.append(slots[position])
    for index, slot in enumerate(ordered):
        slot.index = index
    return ordered


def _engines(slot: Slot) -> set[str]:
    return {engine for entries in slot.votes.values() for engine, _w, _c in entries}


def reconcile(hypotheses: list[EngineHypothesis], *, dispute_margin: float = DISPUTE_MARGIN) -> RoverResult:
    """Vote across engines. Pure, deterministic, and replayable."""
    result = RoverResult(engines=tuple(h.engine for h in hypotheses))
    if not hypotheses:
        return result
    if len(hypotheses) == 1:
        only = hypotheses[0]
        result.tokens = only.tokens()
        result.text = only.text
        result.slot_count = len(result.tokens)
        result.unanimous_slots = result.slot_count
        return result

    network = build_network(hypotheses)
    total_weight = sum(h.weight for h in hypotheses)
    result.slot_count = len(network)

    for slot in network:
        ranked = slot.ranked(total_weight)
        if not ranked:
            continue
        winner, winner_score = ranked[0]
        runner_up_score = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = round(winner_score - runner_up_score, 4)

        # Unanimity is about the *tokens*, not the score: every engine voting
        # the same way is the signal, and a tie in score is not the same thing.
        if len(slot.votes) == 1:
            result.unanimous_slots += 1
        elif margin < dispute_margin:
            result.disputed.append(DisputedSpan(slot_index=slot.index, candidates=tuple(ranked[:3]), margin=margin, left_context=" ".join(result.tokens[-6:]), right_context=""))

        if winner != NULL:
            result.tokens.append(winner)

    # Right context is only knowable once the tokens after a dispute exist.
    result.disputed = [DisputedSpan(slot_index=d.slot_index, candidates=d.candidates, margin=d.margin, left_context=d.left_context, right_context=" ".join(result.tokens[len(d.left_context.split()) + 1 :][:6])) for d in result.disputed]
    result.text = " ".join(result.tokens)

    log.info("rover_reconciled", engines=list(result.engines), slots=result.slot_count, disagreement_score=result.disagreement_score, disputed=len(result.disputed), dispute_rate=result.dispute_rate)
    return result


def arbitration_payload(result: RoverResult, *, max_spans: int = 25) -> list[dict[str, object]]:
    """The disputed spans, and nothing else, for the LLM arbitrator."""
    return [{"slot_index": span.slot_index, "candidates": [{"token": t or "<silence>", "score": s} for t, s in span.candidates], "margin": span.margin, "left_context": span.left_context, "right_context": span.right_context, "is_deletion_dispute": span.is_deletion_dispute} for span in result.disputed[:max_spans]]


def apply_arbitration(result: RoverResult, decisions: dict[int, str]) -> RoverResult:
    """Replace disputed tokens with the arbitrator's choices."""
    if not decisions:
        return result

    network_positions = {span.slot_index for span in result.disputed}
    tokens: list[str] = []
    slot_cursor = 0
    token_cursor = 0

    while token_cursor < len(result.tokens):
        while slot_cursor in network_positions and slot_cursor not in decisions:
            slot_cursor += 1
        if slot_cursor in decisions:
            chosen = decisions[slot_cursor]
            if chosen:
                tokens.append(chosen)
            token_cursor += 1
            slot_cursor += 1
            continue
        tokens.append(result.tokens[token_cursor])
        token_cursor += 1
        slot_cursor += 1

    result.tokens = tokens
    result.text = " ".join(tokens)
    return result


def from_asr_results(results: dict[tuple[str, str], ASRResult], *, weights: dict[str, float] | None = None) -> list[EngineHypothesis]:
    """`{(engine, version): ASRResult}` → hypotheses, with bake-off weights."""
    weights = weights or {}
    return [EngineHypothesis(engine=engine, engine_version=version, text=result.text, words=tuple(result.words), weight=weights.get(engine, 1.0)) for (engine, version), result in results.items()]
