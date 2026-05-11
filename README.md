# Chief of Staff

> A chief of staff for your AI. You delegate. It manages. You only hear back when something genuinely needs you.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![main: v1.0](https://img.shields.io/badge/main-v1.0-success.svg)](https://github.com/goyaljai/chief-of-staff/releases/tag/v1.0)
[![develop: v2.0-phase2.5](https://img.shields.io/badge/develop-v2.0--phase2.5-orange.svg)](https://github.com/goyaljai/chief-of-staff/tree/develop)
[![CI](https://github.com/goyaljai/chief-of-staff/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/goyaljai/chief-of-staff/actions/workflows/ci.yml)
[![Eval baseline](https://img.shields.io/badge/eval--baseline-16/16-success.svg)](eval/results/baseline_v3_5.json)

---

## The problem

Every AI coding tool today assumes **you are the supervisor.**

Claude Code asks questions mid-task. Says "done" when it isn't. Drifts from the goal. Marks checkboxes as evidence. You're not saving time — you're trading writing code for **managing an AI**.

The bottleneck isn't speed. It's the **admin tax** of being the supervisor.

## What this is

**Chief of Staff** is a management layer that sits between you and Claude Code.

Give it any task — code, research, writing, analysis — via **Telegram**, **web UI**, or **curl**. It asks 3-5 sharp upfront questions. Disappears. Manages Claude end-to-end. Comes back when it's done. **Only interrupts you when it genuinely needs a human decision.**

```
YOU (Telegram / web UI / curl)
   │  one task, 3-5 answers
   ▼
ORCHESTRATOR (Databricks LLM, your manager)
   │  meta-thinks → SKILL brief → executor brief → optional ## Steps DAG
   ▼
LANGGRAPH DAG EXECUTOR        ←  parallel fan-out when steps are independent
   │  one Claude subprocess per step + per-step hook log isolation
   │  PostgresSaver checkpoints every superstep (resume on crash)
   ▼
CLAUDE CODE (headless, doer)
   │  every action streams back via SSE; permission_hook enforces boundaries
   ▼
REVIEWER (independent QA LLM, blind to brief)
   │  examines workspace artifacts as ground truth
   ▼
3 correction loops → escalate to you only if truly stuck
   │
   ▼
LEARNING LAYER  (skill_lessons table in Postgres)
   Auto-promoted lessons surface in every future task's prompt,
   sorted by how often they've been hit.
```

---

## What's new in v1.0 (today)

The system shipped today as v1.0 with:

- **LangGraph DAG executor** with the canonical map-reduce dispatcher pattern — independent brief steps run in parallel, dependent steps respect topology.
- **Postgres + pgvector** via Supabase replaces SQLite + Chroma. Connection-pooled, batched log writes, transient-error retry, atomic dual-writes with rollback.
- **Structured skill lessons** — DB-backed with frequency UPSERT, JSONB `domains`, optional `remediation`. Frequency-weighted ordering means the most-hit lessons surface first in every prompt.
- **DAG checkpoint + resume** via LangGraph `PostgresSaver`. Server crash mid-DAG no longer loses work — re-invoking the same task ID resumes from the last completed superstep.
- **Graceful shutdown** — SIGINT/SIGTERM drains in-flight work, marks tasks `interrupted` with checkpoint references, kills runner subprocesses cleanly.
- **Audit log + `/undo`** — every Claude file write captured pre-mutation; `POST /task/{id}/undo` restores the workspace.
- **Free-text escalation** — answer escalations with arbitrary text, not just A/B.
- **Dry-run mode** — `POST /task/run?dry_run=true` returns the brief without spawning Claude. Preview before paying tokens.
- **`/admin/promote`** — seed the skill library directly via API (with optional `ADMIN_TOKEN` gate, constant-time compared).
- **20-task eval suite** with frozen 16/16 baseline + GitHub Actions CI on every PR.
- **29 critical bug fixes** across 4 audit rounds — DAG cancel, hook log race, atomic writes, step-id traversal, binary corruption, concurrent write race, and more.

## What's new in v2.0 Phase 1 (on `develop`)

Just shipped this morning:

- **Voyage rerank-2 cross-encoder** — after pgvector cosine search returns top-20 candidates, Voyage re-scores them. Empirically +15-30% retrieval quality on `/ask` and `find_matching_skill`.
- **Databricks `gte-large-en` embeddings (1024-dim)** replace local HuggingFace MiniLM (384-dim). Higher quality, no local model download, uses your existing `DATABRICKS_TOKEN`.
- **40-task eval suite** — 28 fast + 12 slow including 8 deliberately hard tasks designed for the v2.0 Phase 3 DSPy gradient.
- **8 more critical bug fixes** across rounds 5-7 — silent data corruption in batch embeddings, no-retry on transient embedding errors, no rerank-score floor, non-atomic dual-writes, compound nested retries, timing-attack-vulnerable token compare, and more.

---

## Quick start (Docker — recommended)

```bash
git clone https://github.com/goyaljai/chief-of-staff.git
cd chief-of-staff
git checkout main           # v1.0 stable
cp .env.example .env

# Edit .env to set:
#   DATABRICKS_TOKEN=...
#   DATABRICKS_BASE_URL=https://your-workspace.gcp.databricks.com/ai-gateway/mlflow/v1
#   DATABASE_URL=postgresql://...               (Supabase or self-hosted Postgres + pgvector)
#   LANGSMITH_API_KEY=...                       (optional — for tracing)
#   VOYAGE_API_KEY=...                          (optional — enables rerank, /ask quality boost)
#   TELEGRAM_BOT_TOKEN=...                      (optional — leave blank to skip Telegram)
#   TELEGRAM_ALLOWED_USER_IDS=                  (populate after first DM)
#   ADMIN_TOKEN=...                             (optional — gates /admin/* endpoints)

# Apply schema migrations
psql "$DATABASE_URL" -f migrations/postgres_v3.sql
psql "$DATABASE_URL" -f migrations/postgres_v3_5_b2_skills.sql
psql "$DATABASE_URL" -f migrations/postgres_v2_0_t2_embed_1024.sql   # for v2.0 (develop only)

docker-compose up -d
```

Server at `http://localhost:8000/`. Web UI is the dashboard. Telegram bot starts automatically if token is set.

## Quick start (Python local)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env per above + apply migrations

python3 main.py            # server on :8000
python3 -m integrations.telegram.bot    # bot (separate terminal, optional)
```

---

## Submitting tasks (3 ways)

**1. Web UI** — `http://localhost:8000/` → click **+ New task** → type goal → answer questions → walk away.

**2. Telegram** — DM your bot. The first message tells you your `user_id`; add it to `TELEGRAM_ALLOWED_USER_IDS` and restart.

**3. curl**:

```bash
# Get clarifying questions
curl -X POST http://localhost:8000/task/questions \
  -H 'Content-Type: application/json' \
  -d '{"task":"build a hello world android app in kotlin"}'

# Submit task with answers
curl -X POST http://localhost:8000/task/run \
  -H 'Content-Type: application/json' \
  -d '{
    "task":"build a hello world android app in kotlin",
    "clarifications":{"min_sdk":"24","ui":"XML views","done":"./gradlew assembleDebug succeeds"}
  }'

# Preview the brief WITHOUT spawning Claude (free token-wise — no executor cost)
curl -X POST 'http://localhost:8000/task/run?dry_run=true' \
  -H 'Content-Type: application/json' \
  -d '{"task":"summarize the latest payments regulation in India","clarifications":{}}'
```

---

## What's enforced (not just logged)

A separate process intercepts every Claude tool call **before** it executes:

| Action | Behavior |
|---|---|
| `rm -rf /` or `rm -rf ~` | **BLOCK** (catastrophic) |
| `rm -rf` to a path **outside** the task workspace | **BLOCK** (workspace escape) |
| `Write`/`Edit` to absolute paths outside workspace | **BLOCK** (except `/tmp`) |
| `sudo`, `curl\|bash`, `chmod 777` | **REVIEW** (allowed, flagged) |
| `./gradlew`, `git`, `npm`, `pytest` | **ALLOW** (safelisted) |
| MCP tools (`mcp__server__tool_name`) | **Hooked** — bash/write variants reviewed |

Plus, every `Write` / `Edit` / `MultiEdit` is **snapshotted pre-mutation** to `<workspace>/.cos_snapshots.jsonl`. `POST /task/{id}/undo` walks the snapshots and restores. Binary files are detected and skipped (no corruption from lossy decode + write-back). Concurrent writers serialized via `fcntl.LOCK_EX`.

Hook is per-task in `<workspace>/.claude/settings.json`. Your normal Claude Code usage is untouched.

---

## The learning layer

Every task makes the orchestrator smarter:

```
1. Orchestrator meta-thinks: SKILL brief — what does THIS task need?
2. Asks 3-5 sharp clarifying questions
3. Builds executor brief; if 2+ steps are genuinely independent,
   emits a "## Steps" section that triggers parallel DAG execution
4. Spawns Claude (per step or single subprocess)
5. Watches every action via stream; reviewer flags drift in real time
6. Reviewer reads actual workspace files (ground truth)
7. After completion, generic lessons promoted to the skill_lessons table
```

Lessons are stored in Postgres (`skill_lessons` table) with:
- `pattern_hash` for dedupe across paraphrases
- `frequency` counter incremented on every duplicate promotion
- `domains` JSONB array (`code`, `research`, `data`, `ops`, `writing`)
- Optional `remediation` text — the "how to fix when this rule kicks in"

`skills/global.md` is auto-rendered from the table, sorted by frequency descending. A `[×3]` tag means the lesson has been promoted from 3 separate tasks. The most-impactful lessons surface first in every future orchestrator + reviewer prompt.

Real example of an auto-promoted lesson:
> *"For prose deliverables (blog posts, press releases, narrative essays), do NOT use markdown section headings unless the task explicitly asks for sectioned output. Default to flowing paragraphs. **Fix:** Re-read the task wording: if it says 'blog post', 'essay', 'announcement', treat it as flowing prose."*

---

## Eval suite + CI

```
eval/eval_suite.json        — 40-task suite (28 fast + 12 slow incl. 8 hard) on develop
                             — 20-task suite (16 fast + 4 slow) on main
eval/run_eval.py            — runner: --fast-only, --ab URL_A URL_B, JSON exit code
eval/results/baseline_*.json — frozen baselines per release
eval/test_*.py              — 11 unit + integration suites, all green
.github/workflows/ci.yml    — runs on every PR + push to main/develop
```

Test suites:

| Suite | Cases | Covers |
|---|---|---|
| `test_imports` | module-load smoke | catches missing imports / NameErrors |
| `test_runner_large_output` | 4 | StreamReader 64MB regression |
| `test_chat_retry` | 5 | Databricks 429/5xx/conn retry with jitter |
| `test_critical_fixes` | 7 | D5 case · D6 unique ws · D7 auth · length caps |
| `test_dag_parallel` | 11 | parallel fan-out · diamond topology · validation · isolation · cancel · context enrichment |
| `test_skill_lessons` | 8 | bootstrap · frequency UPSERT · structured input · sorted render · destructive-test guard |
| `test_e5_perf` | 5 | DB pool singleton · 100-row batched insert · async flusher drain |
| `test_f6_checkpoint` | 5 | LangGraph PostgresSaver · same thread_id resume · fresh thread |
| `test_f1_shutdown` | 4 | SIGINT/SIGTERM drain · 503 rejection · interrupt_all_for |
| `test_d1_undo` | 9 | snapshot capture · restore · binary safety · concurrent write race |
| `test_rag_pipeline` | 13 | T1 rerank · T2 embeddings · R5/R6 audit fixes |

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/task/questions` | Get 3-5 clarifying questions |
| `POST` | `/task/run` | Submit task + answers; returns `task_id`. `?dry_run=true` returns brief without spawning Claude |
| `GET` | `/task/{id}` | Full state — status, log, artifacts, `dag_progress`, cost |
| `POST` | `/task/{id}/escalation` | Answer pending escalation. `'a'`/`'b'` (legacy) or any free text |
| `POST` | `/task/{id}/cancel` | Cancel a running task (interrupts all DAG step subprocesses) |
| `POST` | `/task/{id}/resume` | Resume an interrupted task (DAG resumes from last checkpoint) |
| `POST` | `/task/{id}/note` | Add a side-note for an in-flight task (4KB max) |
| `POST` | `/task/{id}/undo` | Restore the workspace to its pre-task state via snapshot replay |
| `POST` | `/task/{id}/ask` | Ask anything about that specific task |
| `GET` | `/task/{id}/stream` | SSE live event tail (filtered for non-DAG signal events) |
| `POST` | `/ask` | Ask history-wide (pgvector + reranker) |
| `POST` | `/admin/reindex` | Bulk re-embed every task (gated by `ADMIN_TOKEN` if set) |
| `POST` | `/admin/promote` | Seed a skill lesson directly (gated by `ADMIN_TOKEN` if set) |
| `GET` | `/tasks` | List tasks |
| `GET` | `/health` | Liveness |

---

## Branch + version policy

| Branch | What | Tag |
|---|---|---|
| `main` | Frozen at v1.0 — closing P0 sprint snapshot | [`v1.0`](https://github.com/goyaljai/chief-of-staff/releases/tag/v1.0) |
| `develop` | Active v2.0 work | `v2.0-phase1` (latest) |

`main` does not receive new commits going forward. v2.0 ships from `develop` after Phases 1-6 + V4 are complete.

---

## v2.0 roadmap

Locked plan — 18 working days from `v2.0-phase1` to `v2.0`.

| Phase | Items | Status |
|---|---|---|
| **Phase 1 — Foundation** | C1.5 eval 16→40 · T1 Voyage rerank · T2 Databricks embeddings | ✅ shipped (`v2.0-phase1`) |
| **Phase 2 — Marquee feature** | B1.5 spike (claude --resume mid-stream) · B1 mid-stream interrupt + inject · B1.6 agnostic tool-review classifier | ✅ shipped |
| **Phase 2.5** | B4 conditional reviewer self-check every 3-4 tool calls | ✅ shipped |
| **Phase 3 — Self-improving** | T3 DSPy auto-optimize prompts · T4 Mem0 agent memory · T5 Promptfoo eval-harness in CI · G5 DSPy-mined skill templates · G7 formal env-can't-deliver escalation patterns · G8 env-audit step at task start | ⏳ next (tomorrow) |
| **Phase 4 — UX polish** | D8b dashboard live progress (DAG accordion) · D5b in-UI free-text escalation · D10 SSE auto-reconnect · F2 backup/export CLI · G6 task-template picker in New-task modal · workspace-TTL cleanup | ⏳ |
| **Phase 5 — Observability** | E1 JSON logs · E2 Prometheus `/metrics` · E6 LangSmith trace test in CI | ⏳ |
| **Phase 6 — Shareable** | README + Loom demo · CONTRIBUTING.md · optional Modal deploy | ⏳ |

### Why each item

- **B1 (mid-stream interrupt)**: today the reviewer's "Wrong action" warnings only land at correction-loop boundaries — too late. B1 lets the supervisor inject coaching mid-stream via `claude --resume`. Real-time coaching is the killer differentiator.
- **T3 (DSPy)**: the 40-task eval suite gives DSPy a real gradient. Programmatic prompt optimization replaces hand-tuning. Stanford framework.
- **T4 (Mem0)**: replaces our skill_lessons + RAG hybrid with one proper agent-memory layer. Frontier of the agent-memory research direction.
- **F6/F1/E5** (already shipped): the unsexy work that makes the system feel solid in production.

---

## Architecture

```
chief-of-staff/
├── main.py                FastAPI server, async task management, F1 graceful shutdown
├── integrations/telegram/  Telegram bot (long polling, no public URL needed)
├── orchestrator.py        Orchestrator + Reviewer + skill_lessons UPSERT
├── claude_runner.py       Headless Claude Code wrapper (64MB stream, --resume, SIGKILL escalation)
├── permission_hook.py     PreToolUse hook — OS-level enforcement + D1 snapshots
├── supervisor_loop.py     The supervision loop (sequential path)
├── dag_executor.py        LangGraph DAG executor (map-reduce dispatcher) + PostgresSaver checkpointing
├── task_store.py          Hydrating store + E5 batched log flusher
├── db.py                  Postgres + pgvector + skill_lessons + connection pool (E5)
├── rag.py                 Databricks gte embeddings (T2) + Voyage rerank (T1) + PGVector
├── config.py              Env config + .env loading
├── prompts/
│   ├── orchestrator.md    Manager prompt
│   └── reviewer.md        Independent QA prompt
├── skills/
│   └── global.md          Universal patterns + auto-promoted lessons (rendered from skill_lessons table)
├── static/
│   └── index.html         Dashboard + Ask UI (D8 step-event accordion)
├── migrations/
│   ├── postgres_v3.sql                    — initial schema
│   ├── postgres_v3_5_b2_skills.sql        — skill_lessons table
│   └── postgres_v2_0_t2_embed_1024.sql    — embeddings 384 → 1024 dim
├── eval/                  20/40-task suite + 11 unit suites + result baselines
├── .github/workflows/     CI workflow with Postgres+pgvector service container
├── diagrams/v2/           Excalidraw architecture + lifecycle
├── Dockerfile, docker-compose.yml
└── prd_doc.md             Original product spec
```

---

## Validated against (real tasks, real artifacts)

- **React Native + WebView app** — Expo + react-native-webview installed, App.tsx written, verified end-to-end (3 DAG steps, parallel where independent).
- **Hello World Android** — built a real APK in 1 loop; reviewer caught Gradle workarounds.
- **5 best mango varieties research** — reviewer rejected first attempt for missing verification, accepted second.
- **Webapp + connect Android app** — multi-step task, completed in 3 loops.
- **Python CLI calculator** — passed loop 1.
- **Postgres vs SQLite research report** — 9KB markdown produced.
- **Frozen v1.0 baseline**: 16/16 PASS at `eval/results/baseline_v3_5.json`.

---

## Tech stack

| Layer | What |
|---|---|
| LLM | Claude Opus 4.7 (Databricks AI Gateway) |
| Orchestration | LangGraph + custom map-reduce dispatcher |
| RAG | LangChain PGVector + Databricks `gte-large-en` embeddings + Voyage `rerank-2` |
| Persistence | Supabase Postgres + pgvector + pg_trgm |
| Observability | LangSmith tracing |
| Server | FastAPI + uvicorn |
| Frontend | Vanilla JS, single-file `index.html` |
| Telegram | python-telegram-bot (long polling) |
| Tests | Plain Python — no pytest required |
| CI | GitHub Actions with Postgres+pgvector service container |

---

## Contributing

Work happens on `develop`. PRs should:

1. Add or update tests in `eval/test_*.py`
2. Pass all 11 existing suites
3. Not regress the eval baseline (`run_eval.py --fast-only` must stay green)
4. Update `skills/global.md` if a learning is genuinely cross-task

CI runs every test on every PR. The CI workflow lives at `.github/workflows/ci.yml`.

---

## License

MIT — see [LICENSE](LICENSE).
