"""Tells when two different words mean the same finding ("consolidation" and "infiltrate"), which sound-alike matching cannot.

Order: normalise a term (normalise: case, hyphens, British spellings) -> look up its concept in the
curated set shipped in data/synonyms.csv (concept_of, synonyms_of) -> score a pair (semantic_similarity:
1.0 for the same words, 0.95 for the same concept, 0.0 otherwise) -> optionally ask RadLex, through
BioPortal's API, for a term the local set does not know (RadLexClient, only with BIOPORTAL_API_KEY).

The local set carries no external codes on purpose: an invented SNOMED or RadLex identifier is worse
than none. RadLex identifiers come only from the API and are stored on lexicon_term.radlex_id.
"""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from radreport.core.logging import get_logger

log = get_logger(__name__)

DATA = Path(__file__).resolve().parent / "data" / "synonyms.csv"
SAME_CONCEPT = 0.95

#: British spellings radiologists in India and the UK write, folded to one form before comparing.
_SPELLINGS: tuple[tuple[str, str], ...] = (("haemorrhage", "hemorrhage"), ("haematoma", "hematoma"), ("haemangioma", "hemangioma"), ("oedema", "edema"), ("oesophag", "esophag"), ("faecal", "fecal"), ("faecolith", "fecalith"), ("ischaem", "ischem"), ("anaemia", "anemia"), ("tumour", "tumor"))


def normalise(term: str) -> str:
    text = term.lower().strip()
    for british, american in _SPELLINGS:
        text = text.replace(british, american)
    text = re.sub(r"[-_/]", " ", text)
    text = re.sub(r"[^a-z0-9 ]", "", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True, slots=True)
class Concept:
    key: str
    preferred: str
    terms: frozenset[str]


@lru_cache(maxsize=1)
def _concepts() -> tuple[dict[str, Concept], dict[str, str]]:
    concepts: dict[str, Concept] = {}
    index: dict[str, str] = {}
    with DATA.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            terms = frozenset(normalise(t) for t in [row["preferred"], *row["synonyms"].split("|")] if t.strip())
            concepts[row["concept"]] = Concept(key=row["concept"], preferred=row["preferred"], terms=terms)
            for term in terms:
                index.setdefault(term, row["concept"])
    return concepts, index


def concept_of(term: str) -> str | None:
    return _concepts()[1].get(normalise(term))


def synonyms_of(term: str) -> frozenset[str]:
    """Every phrasing of the term's concept, normalised; empty when the term is not in the set."""
    key = concept_of(term)
    return _concepts()[0][key].terms if key else frozenset()


def semantic_similarity(a: str, b: str) -> float:
    left, right = normalise(a), normalise(b)
    if left == right:
        return 1.0
    concept = concept_of(left)
    return SAME_CONCEPT if concept is not None and concept == concept_of(right) else 0.0


def pairs() -> list[tuple[str, str, str]]:
    """Every synonym pair in the set, as (concept, a, b)."""
    out = []
    for concept in _concepts()[0].values():
        ordered = sorted(concept.terms)
        out += [(concept.key, a, b) for i, a in enumerate(ordered) for b in ordered[i + 1 :]]
    return out


def markdown() -> str:
    """The set as a table, for the documentation."""
    lines = ["| Concept | Preferred | Also written as |", "|---|---|---|"]
    for concept in sorted(_concepts()[0].values(), key=lambda c: c.preferred):
        others = sorted(t for t in concept.terms if t != normalise(concept.preferred))
        lines.append(f"| `{concept.key}` | {concept.preferred} | {', '.join(others)} |")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class RadLexHit:
    rid: str
    label: str
    synonyms: tuple[str, ...]


class RadLexClient:
    """RadLex through BioPortal's search API. Answers are cached in the shared cache; no key, no calls."""

    BASE = "https://data.bioontology.org/search"

    def __init__(self, *, api_key: str | None = None, client: httpx.Client | None = None) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("BIOPORTAL_API_KEY")
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def lookup(self, term: str) -> RadLexHit | None:
        if not self.enabled:
            return None
        from radreport.cache import shared
        from radreport.cache.keys import GLOBAL, key

        cache_key = key("radlex", GLOBAL, normalise(term)).render()
        cached = shared.get_backend().get(cache_key)
        if cached is not None:
            body = json.loads(cached)
            return RadLexHit(**{**body, "synonyms": tuple(body["synonyms"])}) if body else None
        try:
            client = self._client or httpx.Client(timeout=5)
            response = client.get(self.BASE, params={"q": term, "ontologies": "RADLEX", "require_exact_match": "true", "pagesize": 1}, headers={"Authorization": f"apikey token={self.api_key}"})
            response.raise_for_status()
            hit = _first_hit(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("radlex_lookup_failed", error=type(exc).__name__)
            return None
        shared.get_backend().set(cache_key, json.dumps({"rid": hit.rid, "label": hit.label, "synonyms": list(hit.synonyms)} if hit else {}).encode(), 7 * 24 * 3600)
        return hit


def _first_hit(body: dict[str, Any]) -> RadLexHit | None:
    collection = body.get("collection") or []
    if not collection:
        return None
    first = collection[0]
    rid = str(first.get("@id", "")).rsplit("/", 1)[-1]
    return RadLexHit(rid=rid, label=str(first.get("prefLabel", "")), synonyms=tuple(str(s) for s in first.get("synonym", []) or ()))


if __name__ == "__main__":  # pragma: no cover - writes the documentation table
    print(markdown())
