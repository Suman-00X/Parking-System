"""The pass/fail logic for release gates, and the metric registry behind them."""

from __future__ import annotations

from types import SimpleNamespace

from radreport.eval.gates import Direction, GateMetric, evaluate_gate
from radreport.eval.metrics import default_registry
from radreport.eval.metrics.alignment import align, tokenize


def _run(metrics: dict, *, gate: bool = True, smoke: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id="run", metrics=metrics, is_release_gate=gate, is_smoke_subset=smoke)


def test_a_clean_run_passes() -> None:
    verdict = evaluate_gate(_run({"ROUTE_TOP1": 0.97, "WER": 0.12}), _run({"ROUTE_TOP1": 0.96, "WER": 0.13}))
    assert verdict.passed


def test_direction_is_per_metric() -> None:
    """A single comparison operator gets half of them backwards: ROUTE_TOP1 regressing means down, HALLUC_RATE regressing means up."""
    assert not evaluate_gate(_run({"ROUTE_TOP1": 0.80}), _run({"ROUTE_TOP1": 0.96})).passed
    assert not evaluate_gate(_run({"HALLUC_RATE": 0.09}), _run({"HALLUC_RATE": 0.01})).passed
    assert evaluate_gate(_run({"HALLUC_RATE": 0.005}), _run({"HALLUC_RATE": 0.01})).passed


def test_a_non_gate_run_cannot_approve_a_release() -> None:
    assert not evaluate_gate(_run({"WER": 0.1}, gate=False), None).passed


def test_a_smoke_subset_cannot_approve_a_release() -> None:
    """Fix 2: the ~30-item subset is for iteration, the full set for release candidates."""
    verdict = evaluate_gate(_run({"WER": 0.1}, smoke=True), None)
    assert not verdict.passed
    assert "smoke" in verdict.regressions[0]


def test_a_regression_in_one_group_fails_even_when_the_average_improves() -> None:
    """A regression in one group fails the gate even when the average improves, which is how an average hides the real risk."""
    candidate = _run(
        {
            "ROUTE_TOP1": 0.97,  # average went UP
            "_breakdowns": {
                "by_source_tenant": {
                    "lab-a": {"ROUTE_TOP1": 0.99},
                    "lab-b": {"ROUTE_TOP1": 0.99},
                    "lab-c": {"ROUTE_TOP1": 0.71},  # this lab broke
                }
            },
        }
    )
    baseline = _run({"ROUTE_TOP1": 0.95, "_breakdowns": {"by_source_tenant": {"lab-a": {"ROUTE_TOP1": 0.95}, "lab-b": {"ROUTE_TOP1": 0.95}, "lab-c": {"ROUTE_TOP1": 0.95}}}})
    verdict = evaluate_gate(candidate, baseline)
    assert not verdict.passed
    assert any("lab-c" in r for r in verdict.regressions)


def test_first_run_with_no_baseline_does_not_block() -> None:
    assert evaluate_gate(_run({"WER": 0.14}), None).passed


def test_missing_metrics_block_when_required() -> None:
    """A pipeline that did not measure hallucination has not been shown not to hallucinate."""
    verdict = evaluate_gate(_run({"WER": 0.1}), _run({"WER": 0.1}), require_all=True)
    assert not verdict.passed
    assert "HALLUC_RATE" in verdict.missing


def test_tolerance_admits_noise_but_not_a_real_drop() -> None:
    metric = (GateMetric("ROUTE_TOP1", Direction.HIGHER_IS_BETTER, tolerance=0.01),)
    assert evaluate_gate(_run({"ROUTE_TOP1": 0.955}), _run({"ROUTE_TOP1": 0.96}), metrics=metric).passed
    assert not evaluate_gate(_run({"ROUTE_TOP1": 0.92}), _run({"ROUTE_TOP1": 0.96}), metrics=metric).passed


# --------------------------------------------------------------- metrics ----
def test_wer_and_insertion_rate_are_separate_metrics() -> None:
    """Word error rate and insertion rate stay separate metrics, and this is the evidence for why."""
    keys = default_registry().keys()
    assert "WER" in keys and "INS_RATE" in keys


def test_compliance_and_recall_are_separate_metrics() -> None:
    """Did they say it, versus did we hear it. Only the first decides."""
    keys = default_registry().keys()
    assert "CODEWORD_COMPLIANCE" in keys and "STUDYCODE_RECALL" in keys


def test_task_scoped_runs_only_compute_relevant_metrics() -> None:
    """The per-task swap. A `routing_pick` run has no ASR stage, so scoring WER against it would report an empty metric as a result."""
    registry = default_registry()
    assert [m.key for m in registry.metrics(task_key="routing_pick")] == ["ROUTE_TOP1"]
    assert len(registry.metrics(task_key=None)) == len(registry.keys())


def test_insertions_are_visible_as_a_share_of_errors() -> None:
    """The diagnostic: is the engine mishearing, or inventing?"""
    reference = tokenize("no focal hepatic lesion is seen")
    invented = align(reference, tokenize("no focal hepatic lesion is seen in the right lobe"))
    misheard = align(reference, tokenize("no local hepatic legion is seen"))

    assert invented.insertion_share_of_errors() == 1.0
    assert misheard.insertion_share_of_errors() == 0.0
    assert invented.wer() > 0 and misheard.wer() > 0


def test_alignment_is_deterministic() -> None:
    """Golden-file discipline."""
    a = align(tokenize("the left kidney measures four centimetres"), tokenize("the right kidney measures four cm"))
    b = align(tokenize("the left kidney measures four centimetres"), tokenize("the right kidney measures four cm"))
    assert a == b
