"""Synonym matching: every curated pair matches, different findings do not, and spelling variants fold together."""

from __future__ import annotations

import httpx
import pytest

from radreport.knowledge.synonyms import RadLexClient, concept_of, normalise, pairs, semantic_similarity, synonyms_of


def test_the_set_holds_more_than_fifty_pairs() -> None:
    assert len(pairs()) >= 50


@pytest.mark.parametrize(("concept", "a", "b"), pairs())
def test_every_curated_pair_matches(concept: str, a: str, b: str) -> None:
    assert semantic_similarity(a, b) == 0.95
    assert concept_of(a) == concept_of(b) == concept


@pytest.mark.parametrize(("a", "b"), [("consolidation", "pleural effusion"), ("hepatomegaly", "splenomegaly"), ("pulmonary embolism", "pulmonary edema"), ("renal cyst", "hepatic cyst"), ("atelectasis", "pneumothorax")])
def test_different_findings_do_not_match(a: str, b: str) -> None:
    assert semantic_similarity(a, b) == 0.0


def test_the_roadmaps_example_and_spelling_variants() -> None:
    assert semantic_similarity("consolidation", "infiltrate") == 0.95
    assert semantic_similarity("Intracranial haemorrhage", "brain hemorrhage") == 0.95
    assert normalise("Ground-glass opacity") == "ground glass opacity" and semantic_similarity("ground-glass opacity", "GGO") == 0.95
    assert semantic_similarity("Tree-in-bud", "tree and bud") == 0.95
    assert "fatty liver" in synonyms_of("hepatic steatosis")
    assert synonyms_of("a word nobody uses") == frozenset()


def test_radlex_is_called_only_with_a_key_and_its_answer_is_read() -> None:
    assert not RadLexClient(api_key="").enabled and RadLexClient(api_key="").lookup("consolidation") is None

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "apikey token=k" and request.url.params["ontologies"] == "RADLEX"
        return httpx.Response(200, json={"collection": [{"@id": "http://www.radlex.org/RID/RID99999", "prefLabel": "consolidation", "synonym": ["airspace consolidation"]}]})

    hit = RadLexClient(api_key="k", client=httpx.Client(transport=httpx.MockTransport(handler))).lookup(f"consolidation-{id(handler)}")
    assert hit is not None and hit.rid == "RID99999" and hit.synonyms == ("airspace consolidation",)
