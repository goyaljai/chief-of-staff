# Chief of Staff — Docs

Concept-organized reference. Bug audits don't get their own dump file
— each lesson is rolled into the relevant concept doc so the
documentation stays load-bearing.

| File | Concept |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | High-level: orchestrator → DAG → Claude → reviewer → learning |
| [SCHEMA.md](SCHEMA.md) | Every Postgres table + JSONB shape + index. Auto-checked by drift-check CI test. |
| [PERSISTENCE.md](PERSISTENCE.md) | TaskState ↔ tasks-row mapping. The drift-check rule: every TaskState field except runtime-only ones MUST have a column. |
| [DATA_FLOW.md](DATA_FLOW.md) | Task lifecycle: POST /task/run → SKILL → brief → DAG → loops → review → done |
| [ESCALATION_FLOW.md](ESCALATION_FLOW.md) | G7+ structured ESCALATION:/WHY:/OPTIONS:/ABORT format + parser tightening lessons + B (auto-resolve) classifier |
| [PROBES.md](PROBES.md) | env_audit registry. Adding a new toolchain is a JSON edit, not a code change. |
| [PROMPT_REGISTRY.md](PROMPT_REGISTRY.md) | orchestrator.md, reviewer.md, skill templates per family |
| [EVAL.md](EVAL.md) | Promptfoo gate, run_eval suite, DSPy proxy, why proxy missed today's "wrong inputs to reviewer" bug |
| [TRACING.md](../TRACING.md) | Where every signal goes — LangSmith, Promptfoo dashboard, log_entries table |
| [RUNBOOK.md](RUNBOOK.md) | Common ops: deploy, restart, /undo, /resume, reset baseline |
| [MIGRATIONS.md](MIGRATIONS.md) | Every postgres_v*.sql in chronological order + the "why" for each |
| [CODE_STYLE.md](CODE_STYLE.md) | Comment policy: no comments unless WHY is non-obvious. Module docstrings carry design rationale. |

## How to keep these alive

1. **Bug-audit lessons go into the concept doc**, not into a separate incident log. Today's audit r3 lessons are in `PERSISTENCE.md` (escalation column gap), `ESCALATION_FLOW.md` (parser false-positives), `PROBES.md` (stale env-var paths), and `EVAL.md` (DSPy proxy blind spot).
2. **Drift-check is a CI gate.** `eval/test_schema_drift.py` enforces TaskState ↔ tasks ↔ upsert/hydration consistency. Adding a TaskState field without a column will fail CI.
3. **One concept per doc.** When you find yourself adding a section to `OTHER.md`, ask if a new concept doc would carry the load better.
