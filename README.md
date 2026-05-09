# chief-of-staff

> A chief of staff for your AI. You delegate. It manages. You only hear back when something genuinely needs you.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![V2.5 on main](https://img.shields.io/badge/main-V2.5-success.svg)](prd_doc.md)
[![V3 on develop](https://img.shields.io/badge/develop-V3-orange.svg)](https://github.com/goyaljai/chief-of-staff/tree/develop)

---

## The problem

Every AI coding tool today assumes **you are the supervisor.**

Claude Code asks questions mid-task. Says "done" when it isn't. Drifts from the goal. Marks checkboxes as evidence. You're not saving time — you're trading writing code for **managing an AI**.

The bottleneck isn't speed. It's the **admin tax** of being the supervisor.

## What this is

`chief-of-staff` is a management layer that sits between you and Claude Code.

Give it any task — code, research, writing, analysis — via **Telegram**, **web UI**, or **curl**. It asks 3-5 sharp upfront questions. Disappears. Manages Claude end-to-end. Comes back when it's done. **Only interrupts you when it genuinely needs a human decision.**

```
YOU (Telegram / web UI / curl)
  │  one task, 3-5 answers
  ▼
ORCHESTRATOR (Databricks LLM, your manager)
  │  meta-thinks → SKILL → brief
  ▼
CLAUDE CODE (headless, doer)
  │  every action streams back
  ▼
REVIEWER (independent QA LLM, blind to brief)
  │  examines workspace artifacts as ground truth
  ▼
3 correction loops → escalate to you only if truly stuck
```

## Quick start (Docker — recommended)

```bash
git clone https://github.com/goyaljai/chief-of-staff.git
cd chief-of-staff
cp .env.example .env

# Edit .env to set:
#   DATABRICKS_TOKEN=...
#   DATABRICKS_BASE_URL=https://your-workspace.gcp.databricks.com/ai-gateway/mlflow/v1
#   TELEGRAM_BOT_TOKEN=...        (optional — leave blank to skip Telegram)
#   TELEGRAM_ALLOWED_USER_IDS=    (populate after first DM)

docker-compose up -d
```

Server at `http://localhost:8000/`. Web UI is the dashboard. Telegram bot starts automatically if token is set.

## Quick start (Python local)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env per above
python3 main.py            # server on :8000
python3 telegram_bot.py    # bot (separate terminal)
```

## Submitting tasks (3 ways)

**1. Web UI** — `http://localhost:8000/` → click **+ New task** → type goal → answer questions → walk away.

**2. Telegram** — DM your bot. The first message tells you your `user_id`; add it to `TELEGRAM_ALLOWED_USER_IDS` and restart.

**3. curl**:
```bash
curl -X POST http://localhost:8000/task/questions \
  -H 'Content-Type: application/json' \
  -d '{"task":"build a hello world android app in kotlin"}'

curl -X POST http://localhost:8000/task/run \
  -H 'Content-Type: application/json' \
  -d '{
    "task":"build a hello world android app in kotlin",
    "clarifications":{"min_sdk":"24","ui":"XML views","done":"./gradlew assembleDebug succeeds"}
  }'
```

## What's enforced (not just logged)

A separate process intercepts every Claude tool call **before** it executes:

| Action | Behavior |
|---|---|
| `rm -rf /` or `rm -rf ~` | **BLOCK** (catastrophic) |
| `rm -rf` to a path **outside** the task workspace | **BLOCK** (workspace escape) |
| `Write`/`Edit` to absolute paths outside workspace | **BLOCK** (except `/tmp`) |
| `sudo`, `curl|bash`, `chmod 777` | **REVIEW** (allowed, flagged) |
| `./gradlew`, `git`, `npm`, `pytest` | **ALLOW** (safelisted) |
| MCP tools (`mcp__server__tool_name`) | **Hooked** — bash/write variants reviewed |

Hook is per-task in `<workspace>/.claude/settings.json`. Your normal Claude Code usage is untouched.

## The learning layer

Every task makes the orchestrator smarter:

```
On every task:
  1. Orchestrator meta-thinks: SKILL brief — what does THIS task need?
  2. Asks 3-5 sharp clarifying questions
  3. Builds executor brief, spawns Claude
  4. Watches every action via stream
  5. Reviewer reads actual workspace files (ground truth)
  6. After completion, generic lessons promoted to skills/global.md
```

Real example of an auto-promoted lesson:
> *"For ranked or subjective requests, define the evaluation lens in the brief (fame, prestige, usability, cost, performance) so item selection is consistent and reviewable."*

After 50 tasks, `skills/global.md` is rich and the orchestrator's briefs are sharper. **The product gets smarter with use.**

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/task/questions` | Get 3-5 clarifying questions |
| `POST` | `/task/run` | Submit task + answers; returns `task_id` |
| `GET` | `/task/{id}` | Full state — status, log, artifacts, next_steps, cost |
| `POST` | `/task/{id}/escalation` | Answer pending escalation (`{"answer":"a"}`) |
| `POST` | `/task/{id}/cancel` | Cancel a running task |
| `POST` | `/task/{id}/resume` | Resume an interrupted task (after server restart) |
| `POST` | `/task/{id}/note` | Add a side-note for an in-flight task |
| `POST` | `/task/{id}/ask` | Ask anything about that specific task |
| `GET` | `/task/{id}/stream` | SSE live event tail |
| `POST` | `/ask` | Ask history-wide (FTS5 + ChromaDB) |
| `GET` | `/tasks` | List tasks |
| `GET` | `/health` | Liveness |

## Branch layout

| Branch | Status |
|---|---|
| `main` | V2.5 — stable. Tag for self-hosted users. |
| `develop` | V3 work in progress (critical flaws fixed, bigger features WIP). |

V3 outstanding (planned, not yet merged):
- Postgres + pgvector (replaces SQLite + ChromaDB)
- DAG-based briefs + LangGraph fan-out for parallel exec
- Eval harness for regression tracking
- Mid-loop grounding nudges
- Opt-in anonymized trace upload (foundation for fine-tuning)

See [prd_doc.md](prd_doc.md) for the full V3 spec.

## Architecture

```
chief-of-staff/
├── main.py                FastAPI server, async task management
├── telegram_bot.py        Telegram bot (long polling, no public URL needed)
├── orchestrator.py        Orchestrator + Reviewer (Databricks LLM)
├── claude_runner.py       Headless Claude Code wrapper, --resume, timeout
├── permission_hook.py     PreToolUse hook — real OS-level enforcement
├── supervisor_loop.py     The supervision loop
├── task_store.py          In-memory + SQLite-persisted task state
├── db.py                  SQLite schema + FTS5
├── rag.py                 ChromaDB layer (vector search)
├── config.py              Env config, paths
├── prompts/
│   ├── orchestrator.md    Manager prompt
│   └── reviewer.md        Independent QA prompt
├── skills/
│   └── global.md          Universal patterns + auto-promoted lessons
├── static/
│   └── index.html         Dashboard + Ask UI
├── diagrams/v2/           Excalidraw architecture + lifecycle diagrams
├── Dockerfile
├── docker-compose.yml
└── prd_doc.md             Full product spec
```

## Validated against

- Hello World Android app — built a real APK in 1 loop, reviewer caught Gradle workarounds
- 5 best mango varieties research — reviewer rejected first attempt for missing verification, accepted second
- Webapp + connect Android app to it — multi-step task, completed in 3 loops
- Python CLI calculator — passed loop 1
- Postgres vs SQLite research report — 9KB markdown produced

## License

MIT — see [LICENSE](LICENSE).
