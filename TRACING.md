# Tracing & Observability

Where every Phase 3 component sends its signal.

## LangSmith

Set in `.env`:

```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_pt_...
LANGSMITH_PROJECT=bot               # any project name
LANGCHAIN_TRACING_V2=true            # enables LangGraph tracing
```

| Layer | Traced? | How |
|---|---|---|
| Orchestrator LLM calls (`think_and_ask`, `build_brief`, `generate_skill_brief`, `find_promotable_lessons`, `auto_resolve_escalation`, `parse_dag`) | ✅ | `agents/llm.py` wraps the OpenAI client with `langsmith.wrappers.wrap_openai`. Every `_chat()` is its own LangSmith run. |
| Reviewer LLM calls (`review_action`, `final_review`) | ✅ | Same path as the orchestrator — both go through `agents/llm.py`. |
| LangGraph DAG steps | ✅ | `LANGCHAIN_TRACING_V2=true` makes LangGraph emit a parent run with each node as a child. Filter by graph name. |
| **DSPy optimization** (`eval/dspy/optimize.py`) | ✅ (Phase 3 audit fix) | `_maybe_wire_langsmith()` registers `litellm.success_callback = ["langsmith"]` so every DSPy LLM call is captured. Set `LANGSMITH_PROJECT_DSPY=cos-dspy` to split DSPy runs into their own project. |
| `services/env_audit` subprocess probes | ❌ (no LLM) | Logged to the task's `log_entries` table as `kind='env_audit'`. View via the dashboard task drawer. |
| Escalation parser (`_parse_escalation`) | ❌ (no LLM) | Logged as `kind='executor_escalated'`. |
| Promptfoo CI runs | Separate dashboard | Promptfoo auto-shares to its own dashboard at **app.promptfoo.app/eval** (see the `--share` URL printed in CI logs). To get LangSmith traces of the same calls, run `python eval/dspy/optimize.py` with the same prompts — the DSPy-via-litellm path is langsmith-aware. |

## Quick verify

```bash
# DSPy → LangSmith smoke test
python3 -c "
from dotenv import load_dotenv; load_dotenv()
from eval.dspy.optimize import _configure_lm
import dspy
_configure_lm()
from eval.dspy.signatures import QuestionGenSig
print(dspy.Predict(QuestionGenSig)(task='ping', answers_so_far='{}'))
"
# Then look in app.smith.langchain.com — the run should appear in the
# project named by LANGSMITH_PROJECT_DSPY (or LANGSMITH_PROJECT).
```

## What's NOT traced anywhere

- Telegram bot polling/handlers (logged to stderr only).
- Audit-log snapshots in `<workspace>/.cos_snapshots.jsonl` (filesystem only).
- Skill-lessons UPSERT in Postgres (DB-side, query the `skill_lessons` table directly).

## Tracing the orchestrator + reviewer in prod

Every task currently fires under one LangSmith trace tree per task lifecycle. The trace name is the task ID. To find a specific run:

1. Open `https://smith.langchain.com/o/<org>/projects/p/bot`
2. Filter by metadata `task_id=<id>`
3. Drill in — you'll see meta-think → SKILL brief → executor brief → per-step Claude subprocess (DAG) → reviewer self-checks → final review.
