"""Radiology terms in Hindi, French and Spanish are recognised and given their English, and English text is left alone."""

from __future__ import annotations

import json
import uuid

import httpx

from radreport.knowledge import languages
from radreport.knowledge.languages import GoogleTranslator, LabLanguages, find_foreign, glossary, normalise, translate
from radreport.pipeline.stages.normalise import NormaliseStage
from radreport.pipeline.stages.normalise import glossary as prompt_glossary
from radreport.pipeline.stages.providers import StaticKnowledgeProvider, TenantKnowledge
from radreport.pipeline.state import PipelineState, TranscriptState

HINDI = LabLanguages(languages=("hi",))
ALL = LabLanguages(languages=("hi", "fr", "es"), latin_hindi=True)


def test_devanagari_terms_and_spelling_variants() -> None:
    text = "फेफड़े की सूजन दिखाई दे रही है, गुर्दे की पथरी नहीं।"
    found = {s.surface: s.english for s in translate(text, HINDI).spans}
    assert found == {"फेफड़े की सूजन": "pneumonia", "गुर्दे की पथरी": "renal calculus", "नहीं": "no"}, "the longest phrase wins: kidney stone, not stone"
    assert normalise("गाँठ") == normalise("गांठ") and normalise("फेफड़े") == normalise("फेफडे"), "nasal marks and a dropped nukta still match"
    assert translate(text, HINDI).english_text(text).startswith("pneumonia दिखाई")


def test_latin_hindi_is_opt_in_and_english_is_never_translated() -> None:
    sentence = "Right kidney shows pathri. PET scan advised; normal rate."
    assert translate(sentence, HINDI).spans == []
    assert [(s.surface, s.english) for s in translate(sentence, ALL).spans] == [("pathri", "calculus")], "'pet', 'normal' and 'rate' are English words and never matched"


def test_french_and_spanish_with_or_without_accents() -> None:
    terms = glossary(("fr", "es"))
    assert [s.english for s in find_foreign("Épanchement pleural minime; derrame pleural; esteatosis hepatica", terms)] == ["pleural effusion", "pleural effusion", "hepatic steatosis"]
    assert find_foreign("Coeur de taille normale", terms)[0].english == "heart"


def test_every_bundled_dictionary_loads_and_maps_to_english() -> None:
    for code in languages.SUPPORTED:
        entries = glossary((code,), True)
        assert len(entries) >= 20 and all(e.english.isascii() for e in entries.values())


def test_unknown_words_go_online_only_when_allowed() -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json={"data": {"translations": [{"translatedText": "liver &amp; spleen"} for _ in body["q"]]}})

    online = GoogleTranslator(api_key="test-key", transport=httpx.MockTransport(handler))
    text = "यकृत सामान्य, प्लीहायकृत बढ़ा"
    offline = translate(text, HINDI, translator=online)
    assert sent == [] and offline.unknown == ["प्लीहायकृत", "बढ़ा"], "without the lab's consent nothing leaves"
    allowed = translate(text, LabLanguages(languages=("hi",), online=True), translator=online)
    assert sent[0]["q"] == ["प्लीहायकृत", "बढ़ा"] and sent[0]["source"] == "hi", "single words only, never the sentence"
    assert [s.how for s in allowed.spans] == ["dictionary", "dictionary", "online", "online"] and allowed.spans[2].english == "liver & spleen"

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    failed = translate(text, LabLanguages(languages=("hi",), online=True), translator=GoogleTranslator(api_key="k", transport=httpx.MockTransport(down)))
    assert len(failed.spans) == 2 and failed.unknown, "the dictionaries' answer stands when the service is down"


async def test_normalise_stage_puts_the_english_in_the_extraction_glossary() -> None:
    tenant = uuid.uuid4()
    state = PipelineState(tenant_id=tenant, recording_id=uuid.uuid4(), pipeline_run_id=uuid.uuid4(), transcript=TranscriptState(text="Liver normal. पित्ताशय की पथरी present."))
    stage = NormaliseStage(StaticKnowledgeProvider(TenantKnowledge(tenant_id=tenant, languages=HINDI)))
    result = await stage.run(state, ctx=object())
    assert prompt_glossary(result.output.resolutions) == {"पित्ताशय की पथरी": "gallstone"}
    off = await NormaliseStage(StaticKnowledgeProvider(TenantKnowledge(tenant_id=tenant))).run(state, ctx=object())
    assert off.output.resolutions == []
