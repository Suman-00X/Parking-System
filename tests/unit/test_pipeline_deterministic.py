"""The pipeline stages that must give byte-identical output for identical input, checked against golden files."""

from __future__ import annotations

import uuid

import pytest

from radreport.core.types import TermType
from radreport.knowledge.phonetics import TAU_MARGIN
from radreport.pipeline.stages.normalise import NormaliseStage, glossary, resolve_spans
from radreport.pipeline.stages.providers import LexiconEntry, StaticKnowledgeProvider, StudyCodeEntry, TenantKnowledge
from radreport.pipeline.stages.study_code import compliance_metrics, detect_study_code
from radreport.pipeline.state import PipelineState, StudyCodeDetection, TranscriptState

TENANT = uuid.uuid4()


def _term(canonical: str, *, variants: tuple[str, ...] = (), ambiguous: bool = False):
    from radreport.knowledge.phonetics import double_metaphone

    primary, secondary = double_metaphone(canonical)
    return LexiconEntry(canonical_form=canonical, term_type=TermType.ABBREVIATION, phonetic_key_primary=primary, phonetic_key_secondary=secondary, is_ambiguous=ambiguous, surface_variants=variants)


# ======================================================= margin guard =======
def test_lmc_and_lmp_both_present_escalates_rather_than_picking() -> None:
    """Two sound-alike terms both present escalates instead of picking one, stated as a test."""
    entries = (_term("LMP"), _term("LMC"))
    resolutions = resolve_spans("patient reports L M P as last week", entries)

    assert resolutions, "the spelled run must be recognised as a candidate span"
    spelled = next(r for r in resolutions if "L M P" in r.surface)
    assert spelled.escalated is True
    assert spelled.canonical_form is None
    assert set(spelled.alternatives) == {"LMP", "LMC"}
    assert spelled.margin < TAU_MARGIN


def test_an_unambiguous_term_resolves_without_escalation() -> None:
    """With no confusable neighbour, the same span resolves cleanly."""
    resolutions = resolve_spans("patient reports L M P as last week", (_term("LMP"),))
    spelled = next(r for r in resolutions if "L M P" in r.surface)

    assert spelled.escalated is False
    assert spelled.canonical_form == "LMP"
    assert glossary(resolutions)[spelled.surface] == "LMP"


def test_an_escalated_span_never_reaches_the_glossary() -> None:
    """Handing the extraction model a guess the resolver refused to commit to would launder the ambiguity into a confident answer."""
    entries = (_term("LMP"), _term("LMC"))
    resolutions = resolve_spans("L M P noted", entries)

    assert any(r.escalated for r in resolutions)
    assert glossary(resolutions) == {}


def test_a_pair_a_radiologist_has_not_ruled_on_is_never_auto_resolved() -> None:
    """An unresolved `block`-severity collision means a clinician has been asked and has not answered."""
    entries = (_term("LMP"),)
    clean = resolve_spans("L M P noted", entries)
    assert clean[0].canonical_form == "LMP"

    blocked = resolve_spans("L M P noted", entries, blocked_labels=frozenset({"LMP", "LMC"}))
    assert blocked[0].escalated is True
    assert blocked[0].canonical_form is None


def test_mined_surface_variants_are_matched_as_first_class_candidates() -> None:
    """Verbatim annotation mines how a term actually comes back from ASR."""
    entries = (_term("echotexture", variants=("echo texture",)),)
    resolutions = resolve_spans("liver normal in echo texture throughout", entries)

    assert any(r.canonical_form == "echotexture" for r in resolutions)


def test_resolution_is_order_stable_and_reproducible() -> None:
    """Plan: these stages must be bit-reproducible or the golden files are worthless."""
    entries = (_term("LMP"), _term("USG"), _term("ECG"))
    text = "study type U S G abdomen, L M P last week, E C G normal"

    first = resolve_spans(text, entries)
    second = resolve_spans(text, entries)
    assert [r.model_dump() for r in first] == [r.model_dump() for r in second]
    assert [r.char_start for r in first] == sorted(r.char_start for r in first)


@pytest.mark.asyncio
async def test_normalise_stage_does_not_rewrite_the_transcript() -> None:
    """Char offsets are what provenance cites and what grounding verifies verbatim (I1). Rewriting mid-pipeline re-bases every quote."""
    text = "study type U S G abdomen. L M P last week."
    state = PipelineState(tenant_id=TENANT, recording_id=uuid.uuid4(), pipeline_run_id=uuid.uuid4(), transcript=TranscriptState(text=text))
    provider = StaticKnowledgeProvider(TenantKnowledge(tenant_id=TENANT, lexicon=(_term("LMP"), _term("USG"))))
    result = await NormaliseStage(provider).run(state, ctx=object())

    assert result.output.transcript.text == text, "the transcript must be untouched"
    assert result.output.resolutions
    # And the offsets still point at what they claim to.
    for resolution in result.output.resolutions:
        assert text[resolution.char_start : resolution.char_end] == resolution.surface


def test_a_stage_may_not_read_another_labs_lexicon() -> None:
    """The provider refuses rather than returning an empty snapshot, which would look like "this lab has no lexicon"."""
    provider = StaticKnowledgeProvider(TenantKnowledge(tenant_id=TENANT))
    with pytest.raises(ValueError, match="another lab"):
        provider.for_tenant(uuid.uuid4())


# ==================================================== study-code stage ======
def _code(code: str, spoken: str, *, variants: tuple[str, ...] = ()) -> StudyCodeEntry:
    from radreport.knowledge.phonetics import double_metaphone

    return StudyCodeEntry(template_version_id=uuid.uuid4(), template_code=code, spoken_study_code=spoken, phonetic_key=double_metaphone(spoken)[0], variants=variants)


CODES = (_code("CT_CHEST", "ct chest plain"), _code("USG_ABDOMEN", "usg abdomen"))


def test_a_spoken_code_behind_the_carrier_phrase_routes() -> None:
    detection = detect_study_code("study type ct chest plain, the lungs are", CODES)

    assert detection.carrier_phrase_heard is True
    assert detection.spoken_code_heard == "ct chest plain"
    assert detection.template_version_id == CODES[0].template_version_id
    assert detection.confidence > 0.0


def test_compliance_and_recall_are_distinguishable_failures() -> None:
    """The two have different causes and different fixes, and the most likely response to a bad combined number — abandon the convention — is exactly wrong when the cause is ASR recall."""
    no_carrier = detect_study_code("the lungs are normal in attenuation", CODES)
    assert no_carrier.carrier_phrase_heard is False
    assert no_carrier.spoken_code_heard is None

    carrier_only = detect_study_code("study type mumble mumble something", CODES)
    assert carrier_only.carrier_phrase_heard is True
    assert carrier_only.spoken_code_heard is None


def test_recall_is_conditioned_on_compliance_not_on_the_total() -> None:
    """Dividing by the total lets poor compliance masquerade as poor recall."""
    detections = [StudyCodeDetection(carrier_phrase_heard=True, spoken_code_heard="ct chest"), StudyCodeDetection(carrier_phrase_heard=True, spoken_code_heard=None), StudyCodeDetection(carrier_phrase_heard=False), StudyCodeDetection(carrier_phrase_heard=False)]
    metrics = compliance_metrics(detections)

    assert metrics["CODEWORD_COMPLIANCE"] == 0.5
    # 1 of the 2 compliant dictations was heard — not 1 of 4.
    assert metrics["STUDYCODE_RECALL"] == 0.5


def test_two_confusable_study_codes_refuse_to_route() -> None:
    """The LMC/LMP failure wearing a different hat. Routing to the wrong template is worse than falling through to the cascade."""
    confusable = (_code("A", "L M C"), _code("B", "L M P"))
    detection = detect_study_code("study type L M P then findings", confusable)

    assert detection.spoken_code_heard is not None
    assert detection.escalated is True
    assert detection.template_version_id is None
    assert detection.confidence == 0.0


def test_a_variant_of_the_same_template_is_agreement_not_ambiguity() -> None:
    """Two surfaces of one template scoring close is the mined-variant case. Escalating it would flag every code that verbatim annotation ever improved."""
    entry = _code("CT_CHEST", "ct chest plain", variants=("ct chest",))
    detection = detect_study_code("study type ct chest plain and then", (entry,))

    assert detection.escalated is False
    assert detection.template_version_id == entry.template_version_id


def test_the_search_window_keeps_the_report_body_out_of_routing() -> None:
    """An unbounded search finds "ct chest" in a sentence about a prior."""
    body = "comparison is made with the prior study type ct chest plain from May"
    # The carrier phrase appears here only because the *body* mentions it; a
    # windowed search in the real stage never sees this far in.
    detection = detect_study_code(body[:20], CODES)
    assert detection.carrier_phrase_heard is False
