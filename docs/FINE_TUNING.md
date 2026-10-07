# Fine-tuning the template model on radiologists' approvals

The template model ([TEMPLATE_MODEL.md](TEMPLATE_MODEL.md)) starts as a general instruction
model. Each template a radiologist approves and each sound-alike they confirm or refuse is a
labelled example. This page covers getting those examples out and turning them into a lab-tuned
model.

## 1. Collect: the export

A lab's data is exported only when the lab turns on **System settings → Template import → Share
approvals for model training** (`training.share_approvals`). Then:

```
python -m radreport.devtools.training_export --tenant <lab id> --tenant <lab id> --out exports/training
```

| File | One line per | Used for |
|---|---|---|
| `template_parse.{train,eval}.jsonl` | approved or edited template: system prompt, the document's text, and the schema the radiologist signed off as the answer | fine-tuning the template model (chat format) |
| `variant_pairs.{train,eval}.jsonl` | sound-alike a radiologist answered: heard, term, `same` / `different` | tuning `match_confidence` and the thresholds, or a small classifier |
| `new_terms.{train,eval}.jsonl` | new-term decision: term, `term` / `not_a_term` | tuning the new-term candidate filter |
| `manifest.json` | export | which labs were included, which were skipped for lack of consent, counts |

- What is exported: templates (blank forms), the text of terms, and the decisions.
- What is never exported: report text, transcripts, term contexts (the sentences a term was seen
  in), or anything about a patient.
- Automatic approvals are not exported as labels: they are the system's own guesses.
- Every item lands in the train or eval half by a hash of its id (10% eval), so a re-export never
  moves an item between halves and eval items are never trained on.

Templates uploaded before 2026-10-07 have no stored text (`source_text`) and are not exported.

## 2. Train (LoRA on Qwen 2.5 7B Instruct)

The template-parse files are already in the OpenAI or Hugging Face chat format, so any LoRA
trainer reads them as they are (TRL `SFTTrainer`, Axolotl, Unsloth). Suggested starting point:
rank 16, alpha 32, 2–3 epochs, learning rate 2e-4, max length 4,096 tokens, loss on assistant
turns only. One 24 GB GPU is enough for 4-bit QLoRA on a 7B model.

Wait until there are at least a few hundred approved templates across several labs. Below that, a
fine-tune mostly memorises the labs it saw.

## 3. Evaluate before deploying

1. Serve the base model and the tuned adapter side by side (vLLM:
   `vllm serve Qwen/Qwen2.5-7B-Instruct --enable-lora --lora-modules radreport-templates=./adapter`).
2. Run the fixture harness against each:
   `python -m radreport.devtools.template_eval --endpoint http://127.0.0.1:8000 --model radreport-templates`.
3. Score the `template_parse.eval.jsonl` items the same way (recall and precision of field
   labels against the radiologist's answer).
4. Deploy only if the tuned model beats the base model on both, with precision no lower.

## 4. Deploy

Register the adapter as a model definition (`model_identifier = radreport-templates`, the vLLM
endpoint, prices 0). Propose it for each lab's `template_parse` step and activate it behind a
release-gate evaluation run, as for any model change. The grounding rule still applies to the
tuned model: a label the document does not contain is dropped.

## Status

The export is built and tested. Training, evaluating and deploying a tuned model are not done:
there are no live approvals yet, and no GPU has been set aside for training.
