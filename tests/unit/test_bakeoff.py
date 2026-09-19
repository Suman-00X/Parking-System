"""Scoring and ranking speech engines, including that archive audio never decides the winner."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from radreport.eval.bakeoff import INSERTION_SHARE_DISQUALIFIES, BakeoffReport, EngineResult, PartitionResult, format_report, has_non_latin_script


def _engine(name: str, *, wer: float, insertion_share: float, cter: float | None = None, items: int = 150, legacy_wer: float | None = None) -> EngineResult:
    result = EngineResult(engine=name, engine_version="v1", config_hash="abc")
    result.partitions["current"] = PartitionResult(partition="current", item_count=items, wer=wer, insertion_rate=wer * insertion_share, insertion_share_of_errors=insertion_share, clinical_term_error_rate=cter if cter is not None else wer)
    if legacy_wer is not None:
        result.partitions["legacy"] = PartitionResult(partition="legacy", item_count=100, wer=legacy_wer, insertion_rate=0.01, insertion_share_of_errors=0.10, clinical_term_error_rate=legacy_wer)
    return result


def _report(*engines: EngineResult) -> BakeoffReport:
    return BakeoffReport(eval_set_id=uuid.uuid4(), ran_at=dt.datetime(2026, 9, 25, tzinfo=dt.UTC), engines=list(engines))


def test_an_engine_that_invents_text_loses_to_a_worse_wer() -> None:
    """The actual numbers: even where a model has the better raw word error rate, a high insertion share must sink it."""
    inventor = _engine("large-v3", wer=0.10, insertion_share=0.507)
    honest = _engine("medium", wer=0.132, insertion_share=0.15)

    ranked = _report(inventor, honest).rank()
    assert [r.engine for r in ranked] == ["medium", "large-v3"]
    assert inventor.current.invents_text is True
    assert honest.current.invents_text is False


def test_recommend_returns_none_when_every_engine_invents_text() -> None:
    """Picking the least-bad of four unacceptable engines is a human decision, not something a ranking should do quietly."""
    report = _report(_engine("a", wer=0.10, insertion_share=0.60), _engine("b", wer=0.12, insertion_share=0.55))
    assert report.recommend() is None
    assert "RECOMMENDATION: none" in format_report(report)


def test_clinical_term_errors_break_ties_before_overall_wer() -> None:
    """Keeps CTER separate: a 12% WER of filler words can be clinically perfect, while a 3% WER that drops a "no" is not."""
    chatty = _engine("chatty", wer=0.12, insertion_share=0.1, cter=0.02)
    precise_looking = _engine("precise-looking", wer=0.08, insertion_share=0.1, cter=0.09)

    ranked = _report(chatty, precise_looking).rank()
    assert [r.engine for r in ranked] == ["chatty", "precise-looking"]


def test_legacy_results_never_decide_the_recommendation() -> None:
    """Legacy-archive results never decide the engine recommendation."""
    archive_star = _engine("archive-star", wer=0.30, insertion_share=0.1, legacy_wer=0.05)
    mic_star = _engine("mic-star", wer=0.11, insertion_share=0.1, legacy_wer=0.40)

    report = _report(archive_star, mic_star)
    assert report.recommend().engine == "mic-star"

    rendered = format_report(report)
    assert "archive only, not deciding" in rendered


def test_an_engine_with_no_current_results_cannot_be_recommended() -> None:
    """A legacy-only run answers nothing forward-looking."""
    legacy_only = EngineResult(engine="legacy-only", engine_version="v1", config_hash="x")
    legacy_only.partitions["legacy"] = PartitionResult(partition="legacy", item_count=100, wer=0.05)
    assert _report(legacy_only).recommend() is None


def test_the_insertion_threshold_is_where_7_6_puts_it() -> None:
    """50.7% is the observed large-v3 figure; the threshold must sit below it."""
    assert INSERTION_SHARE_DISQUALIFIES < 0.507


@pytest.mark.parametrize(("text", "expected"), [("liver is normal in echotexture", False), ("liver theek hai लेकिन spleen enlarged", True), ("கல்லீரல் normal", True), ("no non-latin here 123", False)])
def test_code_switching_probe_detects_non_latin_spans(text: str, expected: bool) -> None:
    """Monolingual English ASR on code-switched speech produces confident nonsense rather than failing, so the probe has to be explicit."""
    assert has_non_latin_script(text) is expected


def test_the_report_names_the_recommended_engine_and_its_partition() -> None:
    report = _report(_engine("medium", wer=0.132, insertion_share=0.15), _engine("large-v3", wer=0.190, insertion_share=0.507))
    rendered = format_report(report)

    assert "RECOMMENDATION: medium v1" in rendered
    assert "`current` partition only" in rendered
    assert "invents text" in rendered
