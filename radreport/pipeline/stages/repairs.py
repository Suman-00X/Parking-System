"""Stage 6: resolves a radiologist correcting themselves mid-sentence -- "left, sorry, right kidney".

Order: find the corrections (detect_repairs) -> apply them (apply_repairs) -> keep a record of
what was withdrawn so it cannot resurface later (retracted_spans).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from radreport.core.logging import get_logger
from radreport.core.types import UtteranceLabel
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.state import PipelineState, Utterance

log = get_logger(__name__)

#: Explicit repair cues.
REPAIR_CUES: tuple[str, ...] = (
    r"sorry[,\s]",
    r"correction[,\s]",
    r"scratch that",
    r"strike that",
    r"i mean\b",
    r"i meant\b",
    # "rather," or "or rather" only. Bare "rather" is ordinary speech —
    # "the liver is rather large" is a finding, not a retraction.
    r"or rather\b",
    r"rather,",
    r"no[,\s]+wait",
    r"let me rephrase",
    r"apologies[,\s]",
)
_CUE = re.compile("|".join(f"(?:{c})" for c in REPAIR_CUES), re.IGNORECASE)

#: Words whose replacement is the reason this stage exists. A repair that
#: changes one of these is upgraded to `block` severity in review.
_HIGH_STAKES = re.compile(
    r"\b(left|right|bilateral|midline|no|not|without|absent|present|"
    r"benign|malignant|acute|chronic)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Repair:
    retracted_seq: int
    correcting_seq: int
    cue: str
    changes_high_stakes_term: bool
    retracted_text: str
    correcting_text: str

    split_at: tuple[int, int] | None = None
    """`(cue_start, cue_end)` within the utterance, when the retraction and the correction share one span — "left kidney, sorry, the right kidney"."""


class ResolveRepairsStage:
    """Rewrites labels, never text."""

    name = "resolve_repairs"
    version = "1.0.0"

    def is_idempotent(self) -> bool:
        return True

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if not state.utterances:
            raise ValueError("resolve_repairs runs on segmented utterances; stage 5 has not run")

        repairs = detect_repairs(state.utterances)
        apply_repairs(state.utterances, repairs)

        warnings = [f"repair at utterance {r.retracted_seq}: {r.retracted_text!r} superseded by {r.correcting_text!r} — changes a laterality/negation term" for r in repairs if r.changes_high_stakes_term]

        log.info("repairs_resolved", repairs=len(repairs), high_stakes=sum(1 for r in repairs if r.changes_high_stakes_term), retracted=[r.retracted_seq for r in repairs])
        return StageResult(output=state, confidence=1.0, warnings=warnings)


def detect_repairs(utterances: list[Utterance]) -> list[Repair]:
    """Find cue-marked self-corrections. Pure; safe to replay."""
    repairs: list[Repair] = []
    ordered = sorted(utterances, key=lambda u: u.seq)

    for index, utterance in enumerate(ordered):
        cue = _CUE.search(utterance.text)
        if cue is None:
            continue

        correcting_text = utterance.text[cue.end() :].strip()
        if not correcting_text:
            # The cue ends the utterance — the correction is the next one.
            following = ordered[index + 1] if index + 1 < len(ordered) else None
            if following is None:
                continue
            correcting_seq = following.seq
            correcting_text = following.text
        else:
            correcting_seq = utterance.seq

        retracted = _preceding_content(ordered, index, utterance, cue.start())
        if retracted is None:
            continue

        retracted_seq, retracted_text = retracted
        within = retracted_seq == utterance.seq and correcting_seq == utterance.seq
        repairs.append(Repair(retracted_seq=retracted_seq, correcting_seq=correcting_seq, cue=cue.group(0).strip(), changes_high_stakes_term=_changes_high_stakes(retracted_text, correcting_text), retracted_text=retracted_text, correcting_text=correcting_text, split_at=(cue.start(), cue.end()) if within else None))
    return repairs


def _preceding_content(ordered: list[Utterance], index: int, utterance: Utterance, cue_start: int) -> tuple[int, str] | None:
    """What the cue retracts: text before it, else the previous utterance."""
    before = utterance.text[:cue_start].strip()
    if before:
        return utterance.seq, before

    for candidate in reversed(ordered[:index]):
        if candidate.label == UtteranceLabel.REPORT_CONTENT and candidate.is_included_downstream:
            return candidate.seq, candidate.text
    return None


def _changes_high_stakes(retracted: str, correcting: str) -> bool:
    """Did the repair swap a laterality, a negation or a malignancy term?"""
    before = {m.group(0).lower() for m in _HIGH_STAKES.finditer(retracted)}
    after = {m.group(0).lower() for m in _HIGH_STAKES.finditer(correcting)}
    return bool(before ^ after)


def apply_repairs(utterances: list[Utterance], repairs: list[Repair]) -> None:
    """Mark retracted spans, splitting an utterance where the repair sits inside it."""
    split_targets = {r.retracted_seq: r for r in repairs if r.split_at is not None}

    rebuilt: list[Utterance] = []
    # (retracted utterance object, correcting utterance object) pairs, linked
    # after renumbering — seqs are not final until every split has happened.
    links: list[tuple[Utterance, Utterance]] = []

    for utterance in sorted(utterances, key=lambda u: u.seq):
        repair = split_targets.get(utterance.seq)
        if repair is None or repair.split_at is None:
            rebuilt.append(utterance)
            continue

        cue_start, cue_end = repair.split_at
        retracted_part = utterance.model_copy(update={"char_end": utterance.char_start + cue_start, "text": utterance.text[:cue_start], "label": UtteranceLabel.SELF_CORRECTION, "is_included_downstream": False})
        correcting_part = utterance.model_copy(
            update={
                "char_start": utterance.char_start + cue_end,
                "text": utterance.text[cue_end:],
                # The correction is ordinary report content and must remain
                # groundable — it is what the radiologist actually meant.
                "label": UtteranceLabel.REPORT_CONTENT,
                "is_included_downstream": True,
                "superseded_by_seq": None,
            }
        )
        rebuilt.extend([retracted_part, correcting_part])
        links.append((retracted_part, correcting_part))

    for index, utterance in enumerate(rebuilt):
        utterance.seq = index
    for retracted_part, correcting_part in links:
        retracted_part.superseded_by_seq = correcting_part.seq

    # Cross-utterance repairs: the whole retracted utterance is superseded.
    for repair in repairs:
        if repair.split_at is not None:
            continue
        target = next((u for u in rebuilt if u.text.strip() == repair.retracted_text.strip()), None)
        correcting = next((u for u in rebuilt if u.text.strip().endswith(repair.correcting_text.strip())), None)
        if target is None:
            continue
        target.superseded_by_seq = correcting.seq if correcting else target.seq
        target.is_included_downstream = False
        target.label = UtteranceLabel.SELF_CORRECTION

    utterances[:] = rebuilt


def retracted_spans(utterances: list[Utterance]) -> list[Utterance]:
    """What the review UI renders struck through, with the override shown."""
    return [u for u in utterances if u.superseded_by_seq is not None]
