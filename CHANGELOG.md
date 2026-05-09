# Changelog

## V3 (on `develop` branch — 2026-05-09)

### Critical flaw fixes
- **#1 Per-task usage callback** via `contextvars` — fixes cost attribution under parallel task execution (global callback was overwritten by latest task)
- **#2 Persist Telegram chat_data** via `PicklePersistence` — bot survives restarts without losing mid-conversation state (fixes "BTM" repeat-Q bug)
- **#3 Recoverable task state** — in-flight tasks are marked `interrupted` instead of `abandoned` on hydration; `POST /task/{id}/resume` retries
- **#4 Reviewer truly blind to brief** — `inline_skill=""` passed to `Reviewer.review_action` and `Reviewer.final_review`. Independent judgment restored.

### New capabilities
- **next_steps in `final_review`** — reviewer must include user-facing usage instructions (commands, file paths, "how to actually run this")
- **Skill library match-by-description** — before generating a fresh skill, query Chroma for similar past skills (distance ≤ 0.40); reuse via refinement
- **Per-task `/ask` endpoint** — task-scoped Q&A using only that task's brief, skill, log, artifacts
- **History-wide `/ask`** — FTS5 + ChromaDB merged retrieval, orchestrator synthesizes answer with cited task IDs
- **Side-conversation refinement** — `POST /task/{id}/note` queues mid-task user inputs into the next correction loop
- **Mid-loop grounding nudges** — every 25 `tool_use` events without a verification keyword, orchestrator synthesizes a manager-style 1:1 nudge
- **DAG brief parser** — `Orchestrator.parse_dag` extracts step graph from a `## Steps` section
- **DAG executor** (`dag_executor.py`) — fan-out parallel Claude execution via `asyncio.gather` (LangGraph upgrade pending)
- **Cancel endpoint** — `POST /task/{id}/cancel` interrupts the runner and marks status `cancelled`
- **TodoWrite plan visibility** — Claude's internal todo list surfaced to dashboard

### Operations
- **Docker-compose** — `docker-compose up` brings up server + bot containers with healthcheck
- **LangSmith tracing** (opt-in) — `LANGSMITH_TRACING=true` wraps OpenAI client; traces appear in LangSmith UI
- **Trace upload** (opt-in) — `TRACE_UPLOAD_URL` posts anonymized session traces on completion (foundation for fine-tuning)
- **Postgres + pgvector migration** — schema (`migrations/postgres_v3.sql`) + migration script ready (Postgres consolidates SQLite + ChromaDB)
- **Workspace TTL** — background sweeper deletes workspaces older than `WORKSPACE_TTL_DAYS` (default 30)

### Quality / testing
- **Eval harness** (`eval/run_eval.py`) — 5 reference tasks with structured pass/fail predicates; runs against the live server
- **README polish** — 3-way quickstart (Docker / Python / curl), API table, branch layout, V3 status table
- **Diagrams** — `diagrams/v3/architecture.excalidraw` and `task-lifecycle.excalidraw`

### Bugs fixed during V3 testing
1. Artifact preview cap raised 8 KB → 60 KB (false-fail on 9 KB research report)
2. Artifact list cap raised 8 → 30 files (Android source files truncated out of view)
3. APKs/JARs/etc surfaced as "deliverables" (build/outputs/ was wholly skipped)
4. Skill library match threshold loosened 0.30 → 0.40 (cross-phrasing same-domain misses)
5. MCP auth false-positive tightened (no longer escalates on internal URLs)
6. Live event stream hides on terminal status
7. SKILL.md and Brief panels removed from task detail (internal scaffolding, not user-facing)

### Dashboard polish
- **+ New task** button (full submit flow in modal: questions → answers → run)
- Filter chips (All / Running / Done / Failed) + search input
- Stats bar (total / done / running / total tokens)
- "Started X ago" column instead of meaningless `$0.00`
- Per-row cancel button on running tasks
- 8-second auto-refresh
- Cache-buster on root redirect

---

## V2.5 (on `main` — earlier 2026-05-09)
See `prd_doc.md` for V2.5 scope.

## V2 (initial Telegram + dynamic SKILL.md)
- Telegram bot, dynamic per-task SKILL.md, skill auto-promotion, retry/backoff

## V1 (POC)
- Curl-only intake, supervisor loop, Claude Code headless, PreToolUse hook
