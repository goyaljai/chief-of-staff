# chief-of-staff

> A chief of staff for your AI. You delegate. It manages. You only hear from it when something genuinely needs you.

---

## The problem

Every AI coding tool today assumes **you are the supervisor**.

Claude Code asks questions mid-task. Says "done" when it isn't. Drifts from the original goal. Marks checkboxes as evidence. You're not actually saving time — you're trading writing code for managing an AI.

The bottleneck isn't speed. It's the **admin tax** of being the supervisor.

## What this is

`chief-of-staff` is a management layer that sits between you and Claude Code. You give it a task. It asks ≤5 sharp upfront questions. Disappears. Manages Claude end-to-end. Only interrupts you when it genuinely needs a human decision.

```
YOU
 │  one task, ≤5 answers
 ▼
ORCHESTRATOR ── manager: asks Qs, builds brief, builds corrections
 │
 ▼
CLAUDE CODE ── executor: does the actual work, headlessly
 │  every action streams back
 ▼
REVIEWER ── independent QA: blind to brief, judges only goal vs actions
 │  approve / correct / escalate
 ▼
LOOP
```

Three roles. The user is the supervisor. The LLMs serve them.

## What makes it different

| Product | Has a supervisor layer? | Permission enforcement? | Minimal interruption? |
|---|---|---|---|
| Devin | No — you are | No | Partially |
| Cursor / Copilot | No — you approve every step | No | No |
| LangGraph / CrewAI | DIY | DIY | DIY |
| **chief-of-staff** | **Yes — as a product** | **Yes — real PreToolUse hooks** | **Yes — ≤5 Qs upfront, escalate only when stuck** |

**Core insight:** the supervisor's value is *management instinct*, not domain knowledge. Same questions expose weak work in any domain. Same patterns catch quiet failures whether the work is code, research, writing, or analysis.

## How it actually works

### 1. You hit one endpoint
```bash
curl -X POST http://localhost:8000/task/questions \
  -H "Content-Type: application/json" \
  -d '{"task": "build a hello world android app in kotlin"}'
```

You get back ≤5 questions. Sharp ones — *"what does done mean: builds, or runs end-to-end?"* — not *"what programming language?"*.

### 2. You answer and walk away
```bash
curl -X POST http://localhost:8000/task/run \
  -H "Content-Type: application/json" \
  -d '{
    "task": "...",
    "clarifications": {...},
    "working_dir": "/path/to/your/project"
  }'
```

Returns a `task_id` immediately. Loop runs in the background.

### 3. Behind the scenes
- **Orchestrator** writes a precise brief that an executor can run with no further questions
- **Claude Code** runs headlessly inside an isolated workspace
- Every tool call is intercepted by a **PreToolUse hook** — destructive ops outside the workspace are blocked at the OS level
- Every action streams to the **Reviewer**, an independent LLM call that sees only the goal and the action stream — blind to the brief, so its judgment can't be biased
- If the reviewer finds issues, the orchestrator builds a targeted correction prompt and Claude resumes via session ID with full context preserved
- After 3 correction loops, if still not satisfied, escalates to you with a binary A/B question
- If you don't answer in 30 minutes, the orchestrator makes the best judgment call given the full context

### 4. You come back to a result
- `passed: true` only if the goal has demonstrable verification evidence AND the reviewer found zero issues
- The summary tells you what the reviewer caught that Claude missed — that's the proof of value

## What's enforced (not just logged)

The PreToolUse hook is real OS-level enforcement:

| Action | Behavior |
|---|---|
| `rm -rf /` or `rm -rf ~` | **BLOCK** (catastrophic, no legitimate use) |
| `rm -rf` to a path outside the task workspace | **BLOCK** (workspace escape) |
| `Write`/`Edit` to absolute paths outside workspace | **BLOCK** (except `/tmp`, `/var/tmp`) |
| `sudo`, `curl|bash`, `pip install -<flag>` | **REVIEW** (allowed, flagged for reviewer) |
| `./gradlew`, `git`, `npm`, `pytest`, etc. | **ALLOW** (safelisted dev tools) |
| Unknown commands | **ALLOW** + log for reviewer |

Hook is installed per-task in `<workspace>/.claude/settings.json`. Your normal Claude Code usage is untouched.

## The learning layer

Every task makes the orchestrator smarter. Domain-specific patterns live in `skills/`:

```
skills/
├── general.md   # cross-domain quality patterns (always loaded)
├── android.md   # Android/Gradle pitfalls (auto-loaded for Android tasks)
├── python.md    # ...
├── web.md       # ...
└── research.md  # ...
```

Skills auto-load based on keyword detection in the task. Add a new domain by dropping a `.md` file in `skills/`.

In V2, skills auto-update — every task contributes learnings, the orchestrator gets sharper with use. After 10 tasks of the same type, it stops asking questions it already knows the answer to.

## Quick start

```bash
git clone https://github.com/goyaljai/chief-of-staff.git
cd chief-of-staff

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Set Databricks credentials (or use the default if you have access)
export DATABRICKS_TOKEN="your_token"
export DATABRICKS_BASE_URL="https://your-workspace.gcp.databricks.com/ai-gateway/mlflow/v1"
export DATABRICKS_MODEL="databricks-gpt-5-4"

# Make sure `claude` CLI is installed and authenticated
# https://docs.claude.com/en/docs/claude-code

python3 main.py
```

Server runs at `http://localhost:8000`. See `prd_doc.md` for the full product spec.

## Endpoints (V1)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/task/questions` | Get ≤5 clarifying questions for a task |
| `POST` | `/task/run` | Submit task + answers, returns `task_id` |
| `GET` | `/task/{id}` | Full state — status, log_tail, escalation, result |
| `POST` | `/task/{id}/escalation` | Answer a pending escalation (`{"answer": "a"}` or `"b"`) |
| `GET` | `/tasks` | List all tasks (debug) |
| `GET` | `/health` | Liveness |

## What's in V1 vs V2

**V1 (now):**
- Curl-based API, async task management with task IDs
- Orchestrator (Databricks) → builds briefs, builds corrections, auto-resolves stuck escalations
- Independent Reviewer → judges goal vs action stream, blind to brief
- Claude Code headless executor with `--resume` for correction loops
- Real PreToolUse hooks enforcing workspace boundaries
- Domain skills system (`skills/`)
- Concurrent task support
- Auto-resolve escalations after 30 min

**V2 (next):**
- Telegram bot interface
- Server-Sent Events for live status
- skills.md auto-population (orchestrator writes back learnings)
- Full RAG (SQLite + ChromaDB) — permanent memory across context compactions
- Cancel endpoint
- Cost tracking per task
- Workspace TTL/cleanup
- Permission profile UI
- Web UI

**V3 (later):**
- Parallel execution (fan-out/fan-in)
- Multi-task queue with dependencies
- Fine-tuned orchestrator on real session data — that's the moat

## Architecture

```
chief-of-staff/
├── main.py                    FastAPI server, async task management
├── orchestrator.py            Manager + Reviewer LLM calls (Databricks)
├── claude_runner.py           Headless Claude Code wrapper
├── permission_hook.py         PreToolUse hook — real OS-level enforcement
├── supervisor_loop.py         The supervision loop
├── task_store.py              In-memory task state
├── config.py                  Databricks config, paths
├── prompts/
│   ├── orchestrator.md        Manager prompt — questions, brief, corrections
│   └── reviewer.md            Independent QA prompt — blind reviewer
├── skills/
│   ├── general.md             Cross-domain quality patterns
│   └── android.md             Android/Gradle skills
└── prd_doc.md                 Product requirements doc
```

## Validated against

- **Hello World Android app** — built in 1 loop, 8m52s, APK valid (777KB), reviewer caught real workaround patterns
- **API integration on existing project** — task continues on existing workspace, reviewer caught hidden BUILD SUCCESSFUL line and `GRADLE_USER_HOME` workaround in loop 1, forcing loop 2

The reviewer doesn't pass mediocre work. That's the proof of value.

## License

MIT
