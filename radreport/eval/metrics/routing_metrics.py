"""Measures whether the spoken study code was said, whether it was heard, and whether the right template was chosen.

Defines: how often radiologists say the code (CodewordCompliance), how often it is recognised
(StudyCodeRecall), and how often the top template is the right one (RouteTop1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    pass

NORMALISE_STAGE = "normalise"
ROUTE_STAGE = "route"


class CodewordCompliance:
    """Recordings where a study code was **spoken at all** ÷ total. Per radiologist."""

    key = "CODEWORD_COMPLIANCE"
    owner_stage = "behavioural"
    task_keys: tuple[str, ...] = ()

    def score(self, item, stage_outputs):  # noqa: ANN001, ANN201
        occurrences = item.gold_codeword_occurrences
        if occurrences is None:
            return None, None
        spoken = bool(occurrences.get("study_code_spoken"))
        return float(spoken), {"phrase": occurrences.get("study_code_text")}


class StudyCodeRecall:
    """Study codes spoken and correctly detected ÷ spoken."""

    key = "STUDYCODE_RECALL"
    owner_stage = NORMALISE_STAGE
    task_keys: tuple[str, ...] = ()

    def score(self, item, stage_outputs):  # noqa: ANN001, ANN201
        occurrences = item.gold_codeword_occurrences
        if occurrences is None or not occurrences.get("study_code_spoken"):
            return None, None

        outputs = stage_outputs.get(NORMALISE_STAGE)
        if outputs is None:
            return None, None
        detected = outputs.outputs.get(item.id)
        if detected is None:
            return 0.0, {"detected": None, "expected": occurrences.get("study_code_text")}

        expected = str(occurrences.get("study_code_text", "")).strip().lower()
        actual = str(getattr(detected, "study_code", detected) or "").strip().lower()
        return float(actual == expected and bool(expected)), {"detected": actual, "expected": expected}


class RouteTop1:
    """Template routing accuracy."""

    key = "ROUTE_TOP1"
    owner_stage = ROUTE_STAGE
    task_keys: tuple[str, ...] = ("routing_pick", "routing_shortlist")

    def score(self, item, stage_outputs):  # noqa: ANN001, ANN201
        if item.gold_template_version_id is None:
            return None, None
        outputs = stage_outputs.get(ROUTE_STAGE)
        if outputs is None:
            return None, None
        chosen = outputs.outputs.get(item.id)
        if chosen is None:
            return 0.0, {"chosen": None, "gold": str(item.gold_template_version_id)}

        chosen_id = getattr(chosen, "chosen_template_version_id", chosen)
        return float(str(chosen_id) == str(item.gold_template_version_id)), {"chosen": str(chosen_id), "gold": str(item.gold_template_version_id)}
