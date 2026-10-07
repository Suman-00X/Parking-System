"""Measures how many of a template's fields the parser finds alone, and with the template model behind it.

Order: read each fixture and its expected fields (load_cases) -> parse it, and if the parse is
unsure and a model is given, run the fallback (run_case) -> score found against expected
(score: a field matches when one label's words contain the other's) -> print a table and totals (main).

    python -m radreport.devtools.template_eval
    python -m radreport.devtools.template_eval --endpoint http://127.0.0.1:11434 --model qwen2.5:7b-instruct
"""

from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from radreport.onboarding import template_llm
from radreport.onboarding.template_parse import _title_from_filename, extract_paragraphs, infer_structure

FIXTURES = Path(__file__).parent / "data" / "template_eval"


def _words(label: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9]+", " ", label.lower()).split()) - {"the", "of", "and", "both"}


def _matches(found: str, expected: str) -> bool:
    a, b = _words(found), _words(expected)
    return bool(a and b) and (a <= b or b <= a)


@dataclass(frozen=True, slots=True)
class Score:
    expected: int
    found: int
    correct: int

    @property
    def recall(self) -> float:
        return self.correct / self.expected if self.expected else 1.0

    @property
    def precision(self) -> float:
        return self.correct / self.found if self.found else 0.0


def score(found: list[str], expected: list[str]) -> Score:
    hit = sum(1 for e in expected if any(_matches(f, e) for f in found))
    right = sum(1 for f in found if any(_matches(f, e) for e in expected))
    return Score(expected=len(expected), found=len(found), correct=min(hit, right))


def load_cases(folder: Path = FIXTURES) -> list[tuple[str, bytes, list[str]]]:
    gold = json.loads((folder / "gold.json").read_text())
    return [(name, (folder / name).read_bytes(), labels) for name, labels in sorted(gold.items())]


@dataclass(frozen=True, slots=True)
class CaseResult:
    name: str
    parser: Score
    parser_confidence: float
    combined: Score
    combined_confidence: float
    model_used: bool
    latency_ms: int
    cost_usd: float


def run_case(name: str, data: bytes, expected: list[str], *, fallback: template_llm.Fallback | None, below: float = 0.8) -> CaseResult:
    paragraphs = extract_paragraphs(data, name)
    parsed = infer_structure(paragraphs, fallback_title=_title_from_filename(name))
    alone = score([f.display_label for f in parsed.fields], expected)
    confidence = parsed.confidence
    if fallback is None or parsed.confidence >= below:
        return CaseResult(name, alone, confidence, alone, confidence, False, 0, 0.0)
    started = time.perf_counter()
    outcome = template_llm.apply_fallback(paragraphs, parsed, fallback)
    latency = outcome.latency_ms or int((time.perf_counter() - started) * 1000)
    combined = score([f.display_label for f in outcome.parsed.fields], expected)
    return CaseResult(name, alone, confidence, combined, outcome.parsed.confidence, outcome.used, latency, outcome.cost_usd)


def totals(results: list[CaseResult]) -> dict[str, float]:
    def agg(pick: str) -> tuple[float, float]:
        scores = [getattr(r, pick) for r in results]
        expected, found, correct = sum(s.expected for s in scores), sum(s.found for s in scores), sum(s.correct for s in scores)
        return (correct / expected if expected else 0.0, correct / found if found else 0.0)

    parser_recall, parser_precision = agg("parser")
    combined_recall, combined_precision = agg("combined")
    used = [r for r in results if r.model_used]
    return {"parser_recall": round(parser_recall, 4), "parser_precision": round(parser_precision, 4), "combined_recall": round(combined_recall, 4), "combined_precision": round(combined_precision, 4), "model_calls": len(used), "mean_latency_ms": round(sum(r.latency_ms for r in used) / len(used)) if used else 0, "cost_usd": round(sum(r.cost_usd for r in used), 6)}


def _model(endpoint: str, model: str, api_key: str | None) -> template_llm.Fallback:
    from radreport.adapters.llm.base import ResolvedModelRef
    from radreport.adapters.llm.openai_compat import OpenAICompatibleClient

    ref = ResolvedModelRef(model_definition_id=uuid.uuid4(), model_identifier=model, provider_name="local_openai_compatible", provider_kind="local_openai_compatible", endpoint=endpoint, input_price_per_1k=0.0, output_price_per_1k=0.0, cache_read_price_per_1k=0.0, cache_write_price_per_1k=0.0)
    return template_llm.model_fallback(OpenAICompatibleClient(model_ref=ref, api_key=api_key), model)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", help="an OpenAI-compatible server (vLLM, Ollama, llama.cpp) serving the template model")
    parser.add_argument("--model", default="qwen2.5:7b-instruct")
    parser.add_argument("--api-key")
    parser.add_argument("--below", type=float, default=0.8, help="parser confidence under which the model is asked")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    fallback = _model(args.endpoint, args.model, args.api_key) if args.endpoint else None
    results = [run_case(name, data, expected, fallback=fallback, below=args.below) for name, data, expected in load_cases()]
    summary = totals(results)
    if args.json:
        print(json.dumps({"cases": [{"name": r.name, "parser_recall": round(r.parser.recall, 4), "parser_confidence": r.parser_confidence, "combined_recall": round(r.combined.recall, 4), "combined_precision": round(r.combined.precision, 4), "model_used": r.model_used, "latency_ms": r.latency_ms} for r in results], "totals": summary}, indent=2))
        return
    print(f"{'template':28} {'parser conf':>11} {'parser recall':>13} {'with model':>10} {'precision':>9} {'ms':>6}")
    for r in results:
        print(f"{r.name:28} {r.parser_confidence:11.2f} {r.parser.recall:13.0%} {r.combined.recall:10.0%} {r.combined.precision:9.0%} {r.latency_ms:6}")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
