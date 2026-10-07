"""The template model only adds fields the document names, and the harness measures the difference."""

from __future__ import annotations

import json

from radreport.adapters.llm.base import LLMResponse, Usage
from radreport.devtools import template_eval
from radreport.onboarding import template_llm
from radreport.onboarding.template_parse import infer_structure


def _model(fields: dict[str, list[dict[str, object]]]):  # type: ignore[no-untyped-def]
    """A stand-in model: for each document, the fields it 'reads', keyed by the template title."""

    async def call(paragraphs, parsed):  # type: ignore[no-untyped-def]
        reply = {"title": parsed.title, "sections": ["FINDINGS"], "fields": fields.get(parsed.title, [])}
        return LLMResponse(text=json.dumps(reply), usage=Usage(input_tokens=400, output_tokens=120), model_id="stand-in", latency_ms=240, cost_usd=0.00004)

    return call


PROSE = ["MRI Brain", "The ventricles are normal. No midline shift.", "The pituitary gland measures __ mm.", "Opinion - normal / abnormal."]


def test_only_grounded_fields_are_kept() -> None:
    parsed = infer_structure(PROSE, fallback_title="MRI Brain")
    assert parsed.fields == [] and parsed.confidence == 0.0
    reply = [{"label": "Ventricles", "section": "findings", "data_type": "text"}, {"label": "Pituitary gland", "section": "FINDINGS", "data_type": "measurement", "unit": "mm"}, {"label": "Opinion", "section": "IMPRESSION", "data_type": "enum", "enum_values": ["normal", "abnormal"]}, {"label": "Hippocampal volume", "section": "FINDINGS", "data_type": "measurement"}, {"label": "Status", "section": "FINDINGS", "data_type": "enum", "enum_values": ["x"]}]
    outcome = template_llm.apply_fallback(PROSE, parsed, _model({"MRI Brain": reply}))
    labels = [f.display_label for f in outcome.parsed.fields]
    assert labels == ["Ventricles", "Pituitary gland", "Opinion"], "a label the document never says is dropped"
    assert outcome.fields_dropped == 2 and outcome.fields_added == 3 and outcome.used
    pituitary, opinion = outcome.parsed.fields[1], outcome.parsed.fields[2]
    assert pituitary.data_type == "measurement" and pituitary.unit == "mm" and opinion.enum_values == ("normal", "abnormal")
    assert 0.5 < outcome.parsed.confidence <= template_llm.MERGED_CONFIDENCE_CAP, "a model-read template is never scored as sure as a clean one"
    assert outcome.parsed.modality == "MRI" and outcome.parsed.body_region == "head"
    assert any("read by the template model" in w for w in outcome.parsed.warnings)


def test_parser_fields_win_and_failures_keep_the_parse() -> None:
    parsed = infer_structure(["CT Chest", "Lungs: clear", "Pleura: normal"], fallback_title="CT Chest")
    outcome = template_llm.apply_fallback([], parsed, _model({"CT Chest": [{"label": "Lungs", "section": "FINDINGS", "data_type": "enum", "enum_values": ["a", "b"]}]}))
    assert [f.data_type for f in outcome.parsed.fields] == ["text", "text"] and outcome.fields_added == 0

    async def down(paragraphs, parsed):  # type: ignore[no-untyped-def]
        raise ConnectionError("model server down")

    failed = template_llm.apply_fallback([], parsed, down)
    assert failed.parsed is parsed and not failed.used and "down" in (failed.error or "")


def test_harness_measures_the_gain_on_unstructured_templates() -> None:
    cases = template_eval.load_cases()
    alone = [template_eval.run_case(n, d, e, fallback=None) for n, d, e in cases]
    # The stand-in reads every expected field and invents one per document.
    oracle = {}
    for name, data, expected in cases:
        title = template_eval.infer_structure(template_eval.extract_paragraphs(data, name), fallback_title=template_eval._title_from_filename(name)).title
        oracle[title] = [{"label": e, "section": "FINDINGS", "data_type": "text"} for e in expected] + [{"label": "Invented field", "section": "FINDINGS", "data_type": "text"}]
    helped = [template_eval.run_case(n, d, e, fallback=_model(oracle)) for n, d, e in cases]
    before, after = template_eval.totals(alone), template_eval.totals(helped)
    assert before["parser_recall"] < 0.4 and before["model_calls"] == 0
    assert after["combined_recall"] > 0.9 and after["combined_precision"] == 1.0, "invented fields never survive grounding"
    assert 0 < after["model_calls"] < len(cases), "confident structured templates are not sent to the model"
    assert template_eval.score(["Right kidney"], ["right kidney", "left kidney"]).recall == 0.5
