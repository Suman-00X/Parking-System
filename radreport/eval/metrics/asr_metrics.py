"""The three speech-accuracy measures, all computed from one shared alignment.

Defines: overall word errors (WordErrorRate), invented words counted separately
(InsertionRate), and errors on clinical terms specifically (ClinicalTermErrorRate,
is_clinical_token).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from radreport.eval.metrics.alignment import align, tokenize

if TYPE_CHECKING:  # pragma: no cover
    from radreport.db.models.evaluation import EvalItem
    from radreport.eval.harness import StageOutputs

ASR_STAGE = "asr"


def _hypothesis(stage_outputs: dict[str, StageOutputs], item: EvalItem) -> str | None:
    outputs = stage_outputs.get(ASR_STAGE)
    if outputs is None:
        return None
    value = outputs.outputs.get(item.id)
    if value is None:
        return None
    return value if isinstance(value, str) else getattr(value, "text", None)


class WordErrorRate:
    key = "WER"
    owner_stage = ASR_STAGE
    task_keys: tuple[str, ...] = ()

    def score(self, item, stage_outputs):  # noqa: ANN001, ANN201
        hypothesis = _hypothesis(stage_outputs, item)
        if hypothesis is None or not item.gold_transcript_verbatim:
            return None, None
        alignment = align(tokenize(item.gold_transcript_verbatim), tokenize(hypothesis))
        return alignment.wer(), {"substitutions": alignment.substitutions, "deletions": alignment.deletions, "insertions": alignment.insertions, "reference_length": alignment.reference_length}


class InsertionRate:
    """The hallucination signal, tracked as an **independent** metric."""

    key = "INS_RATE"
    owner_stage = ASR_STAGE
    task_keys: tuple[str, ...] = ()

    def score(self, item, stage_outputs):  # noqa: ANN001, ANN201
        hypothesis = _hypothesis(stage_outputs, item)
        if hypothesis is None or not item.gold_transcript_verbatim:
            return None, None
        alignment = align(tokenize(item.gold_transcript_verbatim), tokenize(hypothesis))
        return alignment.insertion_rate(), {"insertions": alignment.insertions, "share_of_errors": round(alignment.insertion_share_of_errors(), 4)}


#: restricts CTER to the terms whose errors are clinically consequential.
CLINICAL_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\d+(\.\d+)?$"),  # numerals — measurements
    re.compile(r"^(left|right|bilateral|midline)$"),  # laterality
    re.compile(r"^(no|not|without|absent|negative|denies)$"),  # negation cues
    re.compile(r"^(mm|cm|ml|hu|mg)$"),  # units
)

CLINICAL_TERMS: frozenset[str] = frozenset({"liver", "kidney", "spleen", "pancreas", "gallbladder", "uterus", "ovary", "prostate", "bladder", "aorta", "thyroid", "lung", "pleural", "hepatic", "renal", "splenic", "biliary", "adnexal", "lesion", "mass", "cyst", "calculus", "stone", "nodule", "effusion", "pneumothorax", "haemorrhage", "hemorrhage", "dissection", "infarct", "thrombus", "stenosis", "dilatation", "hydronephrosis"})


def is_clinical_token(token: str) -> bool:
    if token in CLINICAL_TERMS:
        return True
    return any(pattern.match(token) for pattern in CLINICAL_TOKEN_PATTERNS)


class ClinicalTermErrorRate:
    """CTER — errors restricted to anatomy, pathology, laterality, numerals and negation cues."""

    key = "CTER"
    owner_stage = ASR_STAGE
    task_keys: tuple[str, ...] = ()

    def score(self, item, stage_outputs):  # noqa: ANN001, ANN201
        hypothesis = _hypothesis(stage_outputs, item)
        if hypothesis is None or not item.gold_transcript_verbatim:
            return None, None

        reference = [t for t in tokenize(item.gold_transcript_verbatim) if is_clinical_token(t)]
        if not reference:
            return None, None

        hypothesis_tokens = [t for t in tokenize(hypothesis) if is_clinical_token(t)]
        alignment = align(reference, hypothesis_tokens)
        return alignment.wer(), {"clinical_reference_length": alignment.reference_length, "substitutions": alignment.substitutions, "deletions": alignment.deletions, "insertions": alignment.insertions}
