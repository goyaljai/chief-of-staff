# Evaluation harness

Three layers of testing, ordered cheapest-to-slowest:

| Layer | What | Cost / run | Cadence |
|---|---|---|---|
| **Schema drift** (`eval/test_schema_drift.py`) | TaskState ↔ tasks ↔ upsert ↔ hydration consistency | <1 s | Every PR |
| **Unit suites** (`eval/test_*.py`, 11 of them) | Module-level invariants — DAG topology, log batching, undo, retry, etc. | seconds | Every PR |
| **Promptfoo gate** (`eval/promptfoo/`) | Orchestrator + reviewer prompts in isolation against curated cases | ~$0.20-0.50 | Every PR |
| **DSPy proxy** (`eval/dspy/optimize.py`) | Prompt optimization against labeled pairs | ~$1-2 | On demand |
| **Full eval** (`eval/run_eval.py`) | E2E with real Claude executor on the 40-task suite | ~$5-10 | Nightly / pre-release |

## Why proxy missed a real bug today

Audit r3 surfaced a bug where `_list_workspace_artifacts` filtered out
`build/` and the APK was sitting under `app/build/outputs/...`. The
reviewer rejected with "APK missing" — structurally correct given its
inputs, but the supervisor was feeding it the wrong window.

DSPy/Promptfoo couldn't catch this. **Why:** my curated workspace listings in
`eval/dspy/dataset.py` and `eval/promptfoo/reviewer.yaml` already
included the APK in the input, e.g. `"- app/build/outputs/apk/debug/app-debug.apk (3.4 MB)"`. The proxy
tested the reviewer prompt; the bug was upstream of the reviewer.

**Lesson:** proxy-metric eval validates prompt quality *given* an input.
It cannot validate that the supervisor builds the right input. That
class of bug is only catchable by E2E (full Claude executor) or
production traffic. Add a real-task gate (a single fast E2E case) to
PR CI to close this gap — flagged in `Phase 6 — Observability` work.

## Drift-check rules

`eval/test_schema_drift.py` enforces:

1. Every `TaskState` field (minus `_RUNTIME_ONLY_FIELDS`) has a column
   on `tasks`.
2. Every `tasks` column appears in `upsert_task`'s INSERT clause.
3. Every persistable field is hydrated in `_load_task_from_db`.

Adding a new TaskState field without all three steps fails the gate.
See [PERSISTENCE.md](PERSISTENCE.md) for the 5-step procedure.

## Promptfoo gate

`eval/promptfoo/{orchestrator,reviewer}.yaml` define curated cases.
Baseline at `eval/promptfoo/baseline.json` is **6/6 = 100%**. CI fails
if the pass rate drops more than 5 pp. Reset the baseline (only after a
deliberate prompt change) with `--init-baseline` — without that flag,
a missing baseline file is a hard error (audit r2 footgun fix).

## DSPy proxy

`eval/dspy/optimize.py` uses BootstrapFewShotWithRandomSearch to mine
demonstrations against `eval/dspy/dataset.py`. Outputs land in
`eval/dspy/optimized/` — these are NOT auto-merged into prod prompts.
A human reviews and copies high-signal entries into
`prompts/orchestrator.md`, `prompts/reviewer.md`, and
`skills/templates/{family}.md`.

`eval/dspy/mine_templates.py` is the G5 prod-history miner — reads up
to 100 completed tasks, groups by family, extracts common acceptance
phrasings + reviewer-flagged gotchas.
