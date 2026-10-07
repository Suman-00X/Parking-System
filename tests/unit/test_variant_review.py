"""How a heard phrase's match to a term is scored and classified."""

from __future__ import annotations

from radreport.knowledge.variant_review import Thresholds, decide, match_confidence


def test_confidence_orders_matches_sensibly() -> None:
    assert match_confidence("echo texture", "echotexture") == 1.0
    assert match_confidence("hepatic steatosis", "fatty liver") == 0.95, "a curated synonym is near-certain"
    assert match_confidence("pleural fusion", "pleural effusion") > match_confidence("nodule", "module") > match_confidence("kidney", "liver")


def test_three_outcomes() -> None:
    limits = Thresholds(auto_approve_above=0.85, review_above=0.6, arm="A")
    assert decide(0.9, limits) == "auto_approved"
    assert decide(0.85, limits) == "pending", "the auto threshold is strict: equal is not above"
    assert decide(0.6, limits) == "pending"
    assert decide(0.59, limits) is None
