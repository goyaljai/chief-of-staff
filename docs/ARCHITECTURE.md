# Architecture

```
USER (Telegram / web UI / curl)
   │  one task, 3-5 adaptive answers (G9)
   ▼
ORCHESTRATOR (Databricks Opus 4.7, agents/orchestrator.py)
   │  meta-think → SKILL brief → env_audit (G8) → executor brief
   │  (with brief-declared deliverables, family templates, env block)
   │  → optional ## Steps DAG section
   ▼
LANGGRAPH DAG EXECUTOR (workflows/dag/dag_executor.py)
   │  parallel fan-out when steps independent; PostgresSaver checkpoints
   ▼
CLAUDE CODE (headless executor, claude_runner.py)
   │  every action streams via SSE; permission_hook enforces boundaries
   │  emits DELIVERABLE_PATHS marker at end (audit r3 deeper-fix)
   ▼
REVIEWER (independent QA, agents/reviewer.py)
   │  fast-path skips LLM on safe actions (C1)
   │  sees: action log + workspace artifacts + brief-declared paths
   │   + executor-declared paths (G10 + audit r3)
   ▼
3 correction loops → escalate (G7+ structured) only if truly stuck
   │
   ▼
LEARNING LAYER
   skill_lessons (Postgres, frequency-weighted)
   pgvector embeddings (task summaries + skill descriptions)
   Mem0 cloud (cross-task agent memory, T4)
```

## Key components

| Component | File | Role |
|---|---|---|
| Supervisor loop | `supervisor_loop.py` | Owns the per-task lifecycle, runs the runner, listens to events, calls reviewer + orchestrator |
| Orchestrator | `agents/orchestrator.py` | Manager — meta-think, questions, brief, correction prompts, auto-resolve |
| Reviewer | `agents/reviewer.py` | Independent QA — per-tool review (with C1 fast-path), drift check, final review (G10 deliverables) |
| Claude runner | `claude_runner.py` | Headless Claude Code wrapper, 64MB stream, --resume, SIGKILL escalation |
| Permission hook | `permission_hook.py` | OS-level pre-tool enforcement + D1 snapshots |
| DAG executor | `workflows/dag/dag_executor.py` | LangGraph map-reduce dispatcher + PostgresSaver checkpointing |
| Task store | `persistence/store.py` | Hydrating store + E5 batched log flusher |
| Tasks repo | `persistence/tasks_repo.py` | upsert_task, append_log, list_tasks (the schema-of-record) |
| RAG | `rag.py` | Databricks gte-large-en embeddings + Voyage rerank + PGVector |
| Memory | `services/memory.py` | T4 Mem0 cloud cross-task memory (fire-and-forget add, timeout-bounded search) |
| Env audit | `services/env_audit.py` + `services/env_probes.json` | G8 toolchain probing (data-driven) |

See [SCHEMA.md](SCHEMA.md) for the persistence layer, [PERSISTENCE.md](PERSISTENCE.md) for the TaskState ↔ DB contract, [ESCALATION_FLOW.md](ESCALATION_FLOW.md) for the G7+ escalation pipeline.
