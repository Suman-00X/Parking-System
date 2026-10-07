# Template model fallback

The template parser reads `Label: value` lines, headings and enum hints. On well-formed Word
templates it finds every field; on prose, bullet lists, tab tables, numbered lists or run-on
capitals it finds none. When the parser's confidence for an upload is below
`templates.llm_fallback_below` (0.80 by default, per lab in **System settings**), the document is
also read by the lab's `template_parse` model.

## What the model is allowed to do

- It returns fields as JSON against a fixed schema (`onboarding/template_llm.py`).
- Every label it gives must appear, word for word, in the document. Anything else is dropped and
  counted (`fields_dropped`), so an invented field never reaches a radiologist.
- The parser's own fields win where both found the same label; the model only adds what the
  parser missed.
- A model-assisted template is scored at most 0.85, so it is never mistaken for a clean parse,
  and the review card says how many fields the model read.
- If the model is down or replies with something other than JSON, the upload still succeeds with
  the parser's result and a warning.
- Each artifact's `parse_warnings.template_model` records whether the model was used, which one,
  fields added and dropped, latency, cost and the parser's own confidence.

`template_parse` is a bounded task: it may run on a local model, because a radiologist reviews
every field before a template goes live.

## Setting it up for a lab

1. Run the model on an OpenAI-compatible server. The recommended starting point is Qwen 2.5 7B
   Instruct:
   - Ollama: `ollama pull qwen2.5:7b-instruct && ollama serve` (endpoint `http://host:11434`)
   - vLLM: `vllm serve Qwen/Qwen2.5-7B-Instruct --max-model-len 16384` (endpoint `http://host:8000`)
   A 7B model at 4-bit fits in about 6 GB of memory; on a single consumer GPU a template-sized
   request takes well under a second, on CPU a few seconds.
2. In the admin panel, add a `local_openai_compatible` provider and a model definition with that
   endpoint (prices 0).
3. Propose it for the lab's `template_parse` step and activate it. Activation needs a release-gate
   evaluation run, as for every model swap.

Gemini Nano is not an option for this: it runs on-device inside Chrome and Android and has no
server API. A hosted small model (for example Gemini Flash-Lite or a hosted Qwen) can be used
through any OpenAI-compatible endpoint, but the client calls `{endpoint}/v1/chat/completions`, so a
provider whose compatible path differs needs a small proxy or a client change.

## Measuring

```
python -m radreport.devtools.template_eval                      # parser only
python -m radreport.devtools.template_eval --endpoint http://127.0.0.1:11434 --model qwen2.5:7b-instruct
```

The harness runs eight fixture templates (`devtools/data/template_eval/`): two structured, one
mixed, five in formats the parser cannot read. A field counts as found when one label's words
contain the other's.

| Measured 2026-10-07 | Recall | Precision |
|---|---:|---:|
| Parser alone | 31% (19 of 62 fields; 0% on the five unstructured formats) | 100% |
| Parser + model | not yet measured: no model server was available on the development machine | |

The harness and the grounding rule are tested with a stand-in model that reads every expected
field and invents one per document: recall rises above 90% and precision stays at 100%. That
proves the mechanics, not a real model's accuracy. Run the second command against a real model,
and against each pilot lab's own templates, before relying on it.
