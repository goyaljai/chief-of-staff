# chief-of-staff

> A chief of staff for your AI. You delegate. It manages. You only hear back when something genuinely needs you.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![V2](https://img.shields.io/badge/version-V2-success.svg)](prd_doc.md)
[![Telegram](https://img.shields.io/badge/interface-Telegram-blue.svg)](https://core.telegram.org/bots)

---

## The problem

Every AI coding tool today assumes **you are the supervisor.**

Claude Code asks questions mid-task. Says "done" when it isn't. Drifts from the goal. Marks checkboxes as evidence. You're not saving time — you're trading writing code for **managing an AI**.

The bottleneck isn't speed. It's the **admin tax** of being the supervisor.

## What this is

`chief-of-staff` is a management layer that sits between you and Claude Code.

You give it any task — code, research, writing, analysis — via **Telegram or curl**. It asks 3-5 sharp upfront questions. Disappears. Manages Claude end-to-end. Comes back when it's done. **Only interrupts you when it genuinely needs a human decision.**

```
YOU (Telegram)
  │  one task, 3-5 answers
  ▼
ORCHESTRATOR (Databricks LLM, your manager)
  │  meta-thinks → SKILL.md → brief
  ▼
CLAUDE CODE (headless, doer)
  │  every action streams back
  ▼
REVIEWER (independent QA LLM, blind to brief)
  │  examines workspace artifacts as ground truth
  ▼
3 correction loops → escalate to you only if truly stuck
```

## What makes it different

| Product | Has supervisor layer? | Real permission enforcement? | Asks you 0 Qs over time? | Open source? |
|---|---|---|---|---|
| Devin | No — you supervise | No | No | No ($500/mo) |
| OpenHands | No — autonomous, you watch | No | No | Yes |
| Cursor / Copilot | No — approves every step | No | No | No |
| LangGraph / CrewAI | DIY | DIY | DIY | Yes (frameworks, not products) |
| **chief-of-staff** | **Yes — as a product** | **Yes — PreToolUse hook + workspace boundaries** | **Yes — auto-promoting skill library** | **Yes (MIT)** |

**Core insight:** the orchestrator's value is *management instinct*, not *domain knowledge*. It generates a fresh `SKILL.md` per task ([Anthropic skill-creator format](https://github.com/anthropics/skills/tree/main/skills/skill-creator)) capturing what *this* task needs — failure patterns, verification criteria, scope boundaries. Same machine handles a code task, a research task, a writing task — orchestrator figures out what each needs.

## What you actually experience

**On Telegram:**

```
You: build a hello world android app in kotlin

Bot: 🤔 Thinking about clarifying questions...
Bot: 📋 I have 3 question(s).
     Q1: What does "done" mean — builds with assembleDebug, or runs end-to-end?

You: builds with assembleDebug

Bot: Q2: Compose or XML views?

You: XML, keep it simple

Bot: Q3: Min SDK?

You: API 24

Bot: Started task 84505ec6f7f2. I'll ping you when there's news.
Bot: What I'll do: Build a complete Android Studio project for a Kotlin
     hello world app with XML views and minSdk 24, ready for assembleDebug.

[~9 minutes later]

Bot: ✅ Task 84505ec6f7f2 done in 532s
     Goal achieved: APK built (777KB, valid Zip with classes.dex +
     AndroidManifest.xml). MainActivity.kt + activity_main.xml in place.
     📁 ~/Desktop/supervisor-workspace/84505ec6f7f2
```

You answered 3 questions. Walked away. Came back to a built APK.

## What's actually enforced (not just logged)

A separate process intercepts every Claude tool call **before** it executes. Real OS-level enforcement, not callback theater.

| Action | Behavior |
|---|---|
| `rm -rf /` or `rm -rf ~` | **BLOCK** (catastrophic, never legitimate) |
| `rm -rf` to a path **outside** the task workspace | **BLOCK** (workspace escape) |
| `Write`/`Edit` to absolute paths outside workspace | **BLOCK** (except `/tmp`, `/var/tmp`, `/private/tmp`) |
| `sudo`, `curl|bash`, `chmod 777`, `pip install -<flag>` | **REVIEW** (allowed, flagged for reviewer) |
| `./gradlew`, `git`, `npm`, `pytest`, `python`, etc. | **ALLOW** (safelisted dev tools) |
| Unknown commands | **ALLOW + log** for reviewer |
| MCP tools (`mcp__server__tool_name`) | **Hooked** — bash/write variants reviewed; auth/read allowed |

Hook is installed per-task in `<workspace>/.claude/settings.json`. **Your normal Claude Code usage outside the supervised loop is untouched.**

## The learning layer (the moat)

Every task makes the orchestrator smarter. **No human curates the skills.**

```
On every task:
  orchestrator.think_and_ask(task)
    → meta-thinks: what does this task need?
    → asks 3-5 sharp clarifying questions

  user answers
    → orchestrator.generate_skill_brief(task, answers, preview)
    → produces <workspace>/skills/SKILL.md
       (Anthropic skill-creator format: YAML frontmatter + body)

  task runs (orchestrator + reviewer both load SKILL.md)

  task completes
    → orchestrator.find_promotable_lessons(task, SKILL.md, log, review)
    → identifies lessons GENERAL enough for FUTURE different tasks
    → appended to skills/global.md (deduped, capped at 50)
```

**Real example of an auto-promoted lesson** (from a research task):

> *"For ranked or subjective requests, define the evaluation lens in the brief (fame, prestige, usability, cost, performance) so item selection is consistent and reviewable."*

That lesson now applies to every future ranking/recommendation task. The system gets sharper with use.

V2.5 will add a skill library — when a new task arrives, the orchestrator first checks if a past `SKILL.md`'s `description` field matches and reuses it before regenerating. After 10 Android tasks, the orchestrator stops asking "Compose or XML?" — it remembers your default.

## Architecture

```
chief-of-staff/
├── main.py                    FastAPI server + async task management
├── telegram_bot.py            Telegram interface (long polling, A/B escalations)
├── orchestrator.py            Orchestrator + Reviewer (Databricks LLM)
├── claude_runner.py           Headless Claude Code wrapper (with timeout)
├── permission_hook.py         PreToolUse hook — real OS-level enforcement
├── supervisor_loop.py         The supervision loop (correction + review + promote)
├── task_store.py              In-memory task state
├── config.py                  Databricks config, paths
├── prompts/
│   ├── orchestrator.md        Manager prompt — Qs, brief, corrections
│   └── reviewer.md            Independent QA prompt — blind to brief
├── skills/
│   └── global.md              Universal patterns + auto-promoted lessons
└── prd_doc.md                 Full product spec
```

**Tech stack:**

| Layer | Technology |
|---|---|
| Orchestrator + Reviewer LLM | Databricks AI Gateway (OpenAI-compatible). 5 SDK retries + 3 transient retries with exp. backoff. |
| Claude Code execution | Claude Code CLI headless, `--output-format stream-json --verbose`, 20-min loop timeout |
| Permission enforcement | PreToolUse hook script, per-task `<workspace>/.claude/settings.json` |
| Correction mechanism | `interrupt()` + `--resume` with session_id + correction prompt |
| Telegram | python-telegram-bot 22.x, long polling (no webhooks needed, no public URL) |
| Per-task skill format | Anthropic skill-creator (YAML frontmatter + body) |
| Orchestration | Plain async/await + asyncio (no LangGraph; overkill for V2 scale) |

## Quick start

```bash
git clone https://github.com/goyaljai/chief-of-staff.git
cd chief-of-staff

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env to add:
#   DATABRICKS_TOKEN=your_token
#   DATABRICKS_BASE_URL=https://your-workspace.gcp.databricks.com/ai-gateway/mlflow/v1
#   TELEGRAM_BOT_TOKEN=from_botfather   (optional — leave blank to skip Telegram)
#   TELEGRAM_ALLOWED_USER_IDS=          (optional — populate after first DM)

# Make sure `claude` CLI is installed and authenticated
# https://docs.claude.com/en/docs/claude-code

# Start the server
python3 main.py    # serves on :8000

# (optional) Start the Telegram bot in a separate terminal
python3 telegram_bot.py
```

DM the bot once to discover your `user_id`, paste it into `TELEGRAM_ALLOWED_USER_IDS`, restart.

### Try it via curl

```bash
# 1. Get clarifying questions
curl -s -X POST http://localhost:8000/task/questions \
  -H "Content-Type: application/json" \
  -d '{"task": "list 5 best mango varieties grown in India with their region and season"}'

# 2. Submit answers
curl -s -X POST http://localhost:8000/task/run \
  -H "Content-Type: application/json" \
  -d '{
    "task": "list 5 best mango varieties grown in India with their region and season",
    "clarifications": {
      "best_meaning": "most famous/premium",
      "format": "small markdown table"
    }
  }'

# 3. Poll for completion
curl -s http://localhost:8000/task/{task_id}
```

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/task/questions` | Get 3-5 clarifying questions + skill_preview |
| `POST` | `/task/run` | Submit task + answers; returns `task_id` |
| `GET` | `/task/{id}` | Full state — status, log_tail, escalation, result, SKILL.md |
| `POST` | `/task/{id}/escalation` | Answer pending escalation (`{"answer": "a"}` or `"b"`) |
| `GET` | `/tasks` | List all tasks (debug) |
| `GET` | `/health` | Liveness |

## Validation

Tested end-to-end against:

- **Hello World Android app** — built a real APK in 1 loop, 8m52s. APK valid (777KB Zip with `classes.dex` + `AndroidManifest.xml`). Reviewer caught real Android failure patterns: `RepositoriesMode.FAIL_ON_PROJECT_REPOS` downgrade, hardcoded `local.properties`.
- **API integration on existing project** — modify-existing flow tested. Reviewer caught hidden `BUILD SUCCESSFUL` line and `GRADLE_USER_HOME` workaround.
- **5 best mango varieties (research)** — passed in 2 loops. Loop 1 rejected for missing visible verification; loop 2 passed with real specifics (Alphonso, Dasheri, Langra, Kesar, Banganapalli — all real famous Indian mango varieties with correct regions and seasons).
- **Skills auto-promotion** — after the research task, 2 generic lessons were promoted to `global.md` automatically. Same skills now apply to every future ranking/research task.

The reviewer doesn't pass mediocre work. **That's the proof of value.**

## What's in V1 vs V2 vs V3

**V1 (shipped):** Curl-based API, async task management, orchestrator, independent reviewer, headless Claude Code, real PreToolUse hooks with workspace boundary enforcement, correction loops, auto-resolve escalations. Validated end-to-end on Hello World Android.

**V2 (~70% shipped):**
- ✅ Telegram bot interface (long polling, single-user whitelist)
- ✅ Dynamic per-task SKILL.md (Anthropic skill-creator format)
- ✅ Skill auto-promotion to `global.md`
- ✅ Reviewer reads workspace artifacts as ground truth
- ✅ Per-action reviewer expanded to Write/Edit/MultiEdit/MCP
- ✅ MCP hook coverage
- ✅ Databricks retry/backoff (5 SDK + 3 explicit)
- ✅ 20-min loop timeout
- ✅ skill_preview cache & reuse
- ✅ Existing-repo support (`working_dir` param)

**V2.5 (next):**
- Skill library + match-by-description (the real "asks 0 Qs after 10 tasks" win)
- RAG layer (SQLite + ChromaDB)
- SSE streaming
- Cost tracking
- Cancel endpoint
- Web UI
- Permission profile UI

**V3:**
- Parallel execution (fan-out/fan-in)
- Multi-task queue with dependencies
- Fine-tuned orchestrator on real session data — *the moat*
- 24/7 hosted backend
- Multi-user with auth + billing

## Positioning

> "A smart manager that never sleeps. Give it any task. Answer 3-5 questions. Come back when it's done."

Not a coding tool. Not a copilot. Not an agent runner.

**A manager.** The first AI product where you are genuinely not the supervisor.

## License

MIT — see [LICENSE](LICENSE).

## Status

V2 — actively developed. See [prd_doc.md](prd_doc.md) for full spec, V2.5/V3 roadmap, and architecture details.

Issues, ideas, and PRs welcome.
