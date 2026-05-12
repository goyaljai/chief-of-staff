# DSPy proxy-metric optimization (T3)

## Why this exists

Phase 3 wanted DSPy auto-optimization for the orchestrator and reviewer
prompts against the 40-task eval suite. Full E2E DSPy optimization
(spinning up Claude executor subprocesses for every candidate prompt)
would cost ~$50-200 per run. We instead ship a **proxy-metric**
optimization that runs ~$1-2 per execution by optimizing the LLM
prompts in isolation against curated `(input, expected_output)` pairs.

If the proxy says a candidate prompt is better, that's strong evidence
the full E2E will agree — but cheap enough to iterate.

## Files

| Path | Purpose |
|---|---|
| `dataset.py` | Hand-labeled `(task, expected)` pairs for question-gen and reviewer |
| `signatures.py` | DSPy `Signature` classes that define the typed input/output shape |
| `optimize.py` | The runner — wires DSPy to Databricks, optimizes both programs, writes artifacts |
| `optimized/question_gen.json` | Compiled program: instructions + bootstrapped few-shot demos |
| `optimized/reviewer.json` | Compiled program for the reviewer |
| `optimized/summary.json` | Before/after metric for each program |

## How to run

```bash
# DATABRICKS_TOKEN + DATABRICKS_BASE_URL must be set (the dotenv loader
# in optimize.py will pick them up from .env automatically).
python eval/dspy/optimize.py
```

Wall-time: ~2 minutes total (both programs).
Cost: ~$1-2 per run on Opus 4.7 (well under the $5-10 budget cap).

## Current state of the gradient

The first run of `optimize.py` reported `delta = +0.0 pp` for both
programs — the dev set hit 100% before optimization. That's diagnostic,
not a failure: it means the existing prompts already solve the curated
proxy cases zero-shot on Opus 4.7. To extract a real DSPy gradient, the
dataset needs to grow to harder cases — likely the 8 deliberately-hard
tasks Phase 1 added to the 40-task eval suite.

## Why ship the infrastructure even with delta=0

1. The harness is real and runnable — when prompt regressions land,
   `optimize.py` will mine demonstrations that fix them.
2. The dataset is the substrate for G5 (DSPy-mined skill templates per
   task family), which depends on T3 infra.
3. The optimized JSON artifacts capture the current prompt state in a
   machine-readable form — useful as a regression fingerprint.

## Promotion path

The compiled `optimized/{question_gen,reviewer}.json` files contain the
final instructions + few-shot demos DSPy chose. To promote them into
prod (a follow-up commit, not part of T3):

1. Read the JSON, copy the `signature.instructions` + `demos` into
   `prompts/orchestrator.md` / `prompts/reviewer.md`.
2. Bump the prompt-version constant in `agents/orchestrator.py`.
3. Re-run the Promptfoo gate (`eval/promptfoo/`) to confirm pass-rate
   doesn't drop.
4. Re-run a curated subset of `run_eval.py` to confirm the full E2E.

## G5 — mining skill templates from prod history

`mine_templates.py` reads completed tasks from Postgres, groups them by
family (code/research/writing/data/ops), and asks DSPy to extract the
recurring acceptance phrasings + gotchas the reviewer actually flagged.

```bash
python eval/dspy/mine_templates.py
```

Output lands at `eval/dspy/optimized/templates/{family}.json`. A human
reviews the JSON and merges high-signal entries into the
`skills/templates/{family}.md` scaffolds — that keeps a human in the
loop on what becomes authoritative guidance for the orchestrator.
