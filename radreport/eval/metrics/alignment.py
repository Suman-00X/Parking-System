"""Lines up a transcript against the ground truth word by word, shared so every speech metric is computed from the same alignment.

Order: split both texts into comparable tokens (tokenize) -> align them (align) into an
Alignment the metrics then read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN = re.compile(r"[a-z0-9']+")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens."""
    return _TOKEN.findall(text.lower())


@dataclass(frozen=True, slots=True)
class Alignment:
    substitutions: int
    deletions: int
    insertions: int
    hits: int
    reference_length: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    def wer(self) -> float:
        if self.reference_length == 0:
            return 0.0
        return self.errors / self.reference_length

    def insertion_rate(self) -> float:
        """The hallucination signal, as a share of reference length."""
        if self.reference_length == 0:
            return 0.0
        return self.insertions / self.reference_length

    def insertion_share_of_errors(self) -> float:
        """How much of the error budget is insertions."""
        return self.insertions / self.errors if self.errors else 0.0


def align(reference: list[str], hypothesis: list[str]) -> Alignment:
    """Levenshtein alignment with per-operation counts."""
    ref_len, hyp_len = len(reference), len(hypothesis)
    # (cost, subs, dels, ins, hits)
    row: list[tuple[int, int, int, int, int]] = [(j, 0, 0, j, 0) for j in range(hyp_len + 1)]

    for i in range(1, ref_len + 1):
        prev = row
        row = [(i, 0, i, 0, 0)]
        for j in range(1, hyp_len + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                cost, subs, dels, ins, hits = prev[j - 1]
                row.append((cost, subs, dels, ins, hits + 1))
                continue
            sub = prev[j - 1]
            dele = prev[j]
            insert = row[j - 1]
            best = min((sub[0] + 1, sub[1] + 1, sub[2], sub[3], sub[4]), (dele[0] + 1, dele[1], dele[2] + 1, dele[3], dele[4]), (insert[0] + 1, insert[1], insert[2], insert[3] + 1, insert[4]), key=lambda entry: entry[0])
            row.append(best)

    _cost, subs, dels, ins, hits = row[hyp_len]
    return Alignment(substitutions=subs, deletions=dels, insertions=ins, hits=hits, reference_length=ref_len)
