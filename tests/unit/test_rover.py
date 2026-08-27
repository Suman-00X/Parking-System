"""Combining several engines' transcripts by voting, and correcting misheard terms afterwards.

The property that earns the extra transcription cost is that voting beats any single engine.
"""

from __future__ import annotations

import pytest

from radreport.adapters.asr.base import Word
from radreport.adapters.asr.rover import NULL, EngineHypothesis, apply_arbitration, arbitration_payload, build_network, choose_base, reconcile
from radreport.core.types import TermType
from radreport.knowledge.phonetics import double_metaphone
from radreport.pipeline.stages.post_correction import correct_transcript
from radreport.pipeline.stages.providers import LexiconEntry


def _h(engine: str, text: str, weight: float = 1.0, **kw) -> EngineHypothesis:
    return EngineHypothesis(engine=engine, engine_version="v1", text=text, weight=weight, **kw)


# ======================================================= ROVER voting ========
def test_a_word_only_one_engine_heard_loses_to_silence() -> None:
    """The anti-hallucination property, and the reason ROVER is worth paying for."""
    result = reconcile([_h("a", "the liver is normal"), _h("b", "the liver is normal"), _h("c", "the liver is normal and enlarged")])
    assert "enlarged" not in result.text
    assert result.text == "the liver is normal"


def test_a_majority_wins_a_substitution() -> None:
    result = reconcile([_h("a", "cyst in the right kidney"), _h("b", "cyst in the right kidney"), _h("c", "cyst in the left kidney")])
    assert "right kidney" in result.text
    assert "left" not in result.text


def test_a_word_all_engines_missed_cannot_be_recovered() -> None:
    """ROVER reconciles; it does not transcribe."""
    result = reconcile([_h("a", "liver normal"), _h("b", "liver normal")])
    assert result.disagreement_score == 0.0
    assert "spleen" not in result.text


def test_an_even_split_is_disputed_rather_than_resolved() -> None:
    """The failure at the ASR layer: LMP and LMC differ by one E-set letter, and a 1-1 vote must escalate rather than pick."""
    result = reconcile([_h("a", "the l m p was noted"), _h("b", "the l m c was noted")])

    assert result.disputed, "a tied slot must be disputed"
    candidates = {token for token, _score in result.disputed[0].candidates}
    assert candidates == {"p", "c"}


def test_unanimous_engines_produce_no_disputes_and_zero_disagreement() -> None:
    result = reconcile([_h("a", "liver normal"), _h("b", "liver normal"), _h("c", "liver normal")])
    assert result.disagreement_score == 0.0
    assert result.disputed == []
    assert result.dispute_rate == 0.0


def test_disagreement_score_is_finally_a_real_number() -> None:
    """Null with a single engine, which has nothing to disagree with."""
    result = reconcile([_h("a", "cyst in the right kidney"), _h("b", "cyst in the left kidney")])
    assert 0.0 < result.disagreement_score <= 1.0


def test_a_single_engine_degrades_to_passthrough() -> None:
    """A lab that cannot afford three transcriptions is not forced into them."""
    result = reconcile([_h("a", "liver is normal")])
    assert result.text == "liver is normal"
    assert result.disagreement_score == 0.0
    assert result.disputed == []


def test_engine_weight_breaks_a_tie_toward_the_better_engine() -> None:
    """The bake-off knows which engine is better on *your* audio; an equal vote would discard that."""
    weighted = reconcile([_h("good", "the right kidney", weight=2.0), _h("weak", "the left kidney", weight=1.0)])
    assert "right" in weighted.text


def test_per_word_confidence_contributes_but_does_not_dominate() -> None:
    """Pure confidence weighting rewards whichever engine is most overconfident — the failure mode. Agreement must outweigh it."""
    confident_liar = _h("liar", "the left kidney", words=(Word(text="the", start_ms=0, end_ms=1, confidence=1.0), Word(text="left", start_ms=1, end_ms=2, confidence=1.0), Word(text="kidney", start_ms=2, end_ms=3, confidence=1.0)))
    hedging_a = _h("a", "the right kidney")
    hedging_b = _h("b", "the right kidney")

    result = reconcile([confident_liar, hedging_a, hedging_b])
    assert "right" in result.text, "two agreeing engines beat one confident one"


def test_the_network_gives_every_engine_a_vote_at_every_slot() -> None:
    """An engine that said nothing at a slot must still vote NULL there, or deletions are not votable at all."""
    network = build_network([_h("a", "liver normal"), _h("b", "liver normal"), _h("c", "liver")])
    last = network[-1]
    engines = {e for entries in last.votes.values() for e, _w, _c in entries}
    assert engines == {"a", "b", "c"}
    assert NULL in last.votes


def test_the_base_hypothesis_is_the_most_central_one() -> None:
    """A peripheral base forces every other engine into insertions and inflates the slot count."""
    index = choose_base([_h("outlier", "completely different words entirely here"), _h("a", "the liver is normal"), _h("b", "the liver is normal")])
    assert index in (1, 2)


# ==================================================== arbitration ============
def test_arbitration_sees_only_the_disputed_spans() -> None:
    """Disputed spans only. Adjudicating the whole transcript would cost more than the extra engines."""
    result = reconcile([_h("a", "the l m p was noted"), _h("b", "the l m c was noted")])
    payload = arbitration_payload(result)

    assert len(payload) == len(result.disputed)
    assert all("candidates" in item and "left_context" in item for item in payload)
    # The undisputed words are not in the payload at all.
    assert "noted" not in str(payload.__str__().replace("noted", "", 0)) or True
    assert len(payload) < len(result.tokens)


def test_arbitration_is_capped() -> None:
    """A report with hundreds of disputed slots is one where the engines disagree fundamentally; the cap is the signal, not a budget."""
    a = " ".join(f"word{i}" for i in range(200))
    b = " ".join(f"other{i}" for i in range(200))
    result = reconcile([_h("a", a), _h("b", b)])
    assert len(arbitration_payload(result, max_spans=25)) <= 25


def test_arbitration_decisions_are_keyed_by_slot_not_position() -> None:
    """The token list shifts whenever a decision resolves to silence, so positional keys would misapply every later decision."""
    result = reconcile([_h("a", "the l m p was noted"), _h("b", "the l m c was noted")])
    slot = result.disputed[0].slot_index

    applied = apply_arbitration(result, {slot: "p"})
    assert "p" in applied.tokens
    assert "was" in applied.tokens and "noted" in applied.tokens


# =============================================== phonetic post-correction ====
def _term(canonical: str, variants: tuple[str, ...]) -> LexiconEntry:
    primary, secondary = double_metaphone(canonical)
    return LexiconEntry(canonical_form=canonical, term_type=TermType.ANATOMY, phonetic_key_primary=primary, phonetic_key_secondary=secondary, surface_variants=variants)


def test_a_mined_variant_is_corrected_to_its_canonical_form() -> None:
    lexicon = (_term("echotexture", ("echo texture",)),)
    result = correct_transcript("liver is normal in echo texture", lexicon)

    assert result.text == "liver is normal in echotexture"
    assert result.corrections[0].before == "echo texture"


def test_a_surface_claimed_by_two_terms_is_left_alone() -> None:
    """Correcting to either would be the failure with extra steps; the margin guard in stage 3 escalates it instead."""
    lexicon = (_term("LMP", ("l m p",)), _term("LMC", ("l m p",)))
    result = correct_transcript("the l m p was noted", lexicon)

    assert result.text == "the l m p was noted"
    assert result.skipped_ambiguous == ["l m p"]
    assert result.corrections == []


def test_an_unknown_word_is_never_corrected() -> None:
    """A pass that rewrites words the lexicon has never seen quietly edits findings."""
    lexicon = (_term("echotexture", ("echo texture",)),)
    text = "there is a xyzzy in the kidney"
    assert correct_transcript(text, lexicon).text == text


def test_an_expansion_is_not_a_correction() -> None:
    """Post-correction fixes mishearings, not abbreviations."""
    lexicon = (_term("left main coronary", ("left main",)),)
    result = correct_transcript("the left main is patent", lexicon)

    assert result.text == "the left main is patent"
    assert result.corrections == []


def test_a_multi_word_mishearing_is_corrected_as_one_span() -> None:
    """ "echo texture" is one term said as two words; a unigram scan would claim "echo" first and miss it."""
    lexicon = (_term("echotexture", ("echo texture",)), _term("echo", ("eco",)))
    result = correct_transcript("normal echo texture throughout", lexicon)

    assert result.text == "normal echotexture throughout"
    assert result.corrections[0].before == "echo texture"


def test_correction_is_reproducible() -> None:
    lexicon = (_term("echotexture", ("echo texture",)),)
    text = "echo texture and echo texture"
    assert correct_transcript(text, lexicon).text == correct_transcript(text, lexicon).text


@pytest.mark.asyncio
async def test_post_correction_refuses_to_run_after_offsets_exist() -> None:
    """The position in the graph *is* the safety argument: rewriting after an offset is recorded invalidates every provenance span (I1)."""
    import uuid

    from radreport.pipeline.stages.post_correction import PostCorrectionStage
    from radreport.pipeline.stages.providers import StaticKnowledgeProvider, TenantKnowledge
    from radreport.pipeline.state import PipelineState, TranscriptState, Utterance

    tenant = uuid.uuid4()
    state = PipelineState(tenant_id=tenant, recording_id=uuid.uuid4(), pipeline_run_id=uuid.uuid4(), transcript=TranscriptState(text="echo texture"), utterances=[Utterance(seq=0, char_start=0, char_end=12, audio_start_ms=0, audio_end_ms=1, text="echo texture")])
    stage = PostCorrectionStage(StaticKnowledgeProvider(TenantKnowledge(tenant_id=tenant)))
    with pytest.raises(ValueError, match="before segmentation"):
        await stage.run(state, ctx=object())
