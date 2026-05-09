# Supervisor — Product Requirements Document

## The Problem

Knowledge workers and vibe coders using Claude Code spend most of their time managing Claude, not doing actual work. Claude asks questions mid-task. Claude says "done" when it isn't. Claude drifts from the original goal. The human is the bottleneck — not because they're slow, but because Claude constantly needs them.

Nobody has built the supervision layer. Every AI coding tool today assumes the human is the supervisor. We remove that assumption.

---

## The Product

**A smart manager that lives in Telegram, never sleeps, and never forgets.**

You give it any task — code, research, writing, analysis. It asks ≤5 questions upfront. Disappears. Manages Claude Code end to end. Comes back when it's done. Only interrupts you when it genuinely needs a human decision.

### The Three Layers

```
YOU
↕ Telegram (only real decisions reach you)

MANAGER / ORCHESTRATOR
- Asks ≤5 upfront questions
- Builds a clear brief
- Independent reviewer — sees output vs goal only
- Watches Claude Code via streaming events
- Corrects drift via interrupt/resume
- Escalates to you only when genuinely stuck
- Saves everything to memory

CLAUDE CODE (Executor)
- Does the actual work headlessly
- Doesn't know it's being reviewed
- Gets corrected via systemMessage/resume
- Never talks to you directly
```

### The Escalation Chain

```
Claude Code hits something
        ↓
Manager knows the answer? → answers, Claude continues
        ↓
Manager doesn't know? → pauses Claude, asks you on Telegram
        ↓
You reply A or B → Manager feeds answer to Claude → resumes
```

Three levels. You only touch Level 3.

---

## What Makes It Different

| Product | Has supervisor? | Permission layer? | Minimal interruption? |
|---|---|---|---|
| Devin | No — you are the supervisor | No | Partially |
| LangGraph | Yes, DIY | No | DIY |
| Lindy AI | No | No | No — approves everything |
| **Supervisor** | **Yes, as a product** | **Yes, judgment-based** | **Yes — Telegram, ≤5 questions** |

**Core insight:** The Supervisor's value is management instinct, not domain knowledge. It asks the universal questions that expose weak work in any domain. Same manager handles code, research, writing, analysis.

---

## Architecture

```
TELEGRAM BOT          — your interface (task intake + escalations)
ORCHESTRATOR LLM      — our LLM via Databricks (generic foundation model today, fine-tuned tomorrow)
CLAUDE CODE SDK       — headless executor, streaming events
PERMISSION LAYER      — PreToolUse hooks, domain-agnostic rules
SUPERVISOR REVIEWER   — independent LLM call, output vs goal only
MEMORY LAYER          — SQLite (structured) + ChromaDB (semantic)
WEB UI                — dashboard, visibility, RAG chat interface
```

### Tech Stack

| Layer | Technology |
|---|---|
| Orchestrator LLM | Databricks AI Gateway (OpenAI-compatible, model: `databricks-gpt-5-4`). Future: fine-tuned model on real session data — that's the moat. |
| Claude Code execution | Claude Code SDK headless + streaming |
| Permission enforcement | PreToolUse hooks |
| Correction mechanism | interrupt() + resume with session_id |
| Orchestration | LangGraph |
| Telegram | python-telegram-bot |
| Structured memory | SQLite |
| Semantic memory | ChromaDB |
| Web UI | React + FastAPI |

### Key Technical Validations

- Claude Code runs headlessly via SDK ✅
- Streaming events give Manager full visibility ✅
- interrupt() + resume = correction mechanism ✅
- Multiple parallel workers via asyncio.gather() ✅
- PreToolUse hooks = permission enforcement ✅
- Session memory persists across context compaction ✅

---

## The Learning Layer

Every task makes the Manager smarter:

```
DURING TASK
  Manager watches stream → learns user preferences, codebase patterns
  Writes to skills.md in real time

AFTER TASK
  Everything saved to RAG: file map, decisions, corrections

NEXT TASK
  Manager reads skills.md + RAG first
  Already knows user preferences
  Already knows codebase patterns
  Asks fewer questions
```

After 10 tasks: Manager asks 0 questions for familiar task types. After 3 months: It's not a generic manager. It's YOUR manager.

### The Data Flywheel

```
More users → more tasks → more data
→ better Orchestrator → better outcomes → more users
```

The fine-tuned Orchestrator is the moat. Nobody else has that dataset.

---

## The RAG Layer

Claude Code's context window fills up. Gets compacted. Session ends. Everything forgotten.

Our RAG layer is permanent memory:

```
SQLite (structured, fast lookups)
  files:      path, purpose, task_id
  decisions:  what, why, alternatives
  preferences: pattern, value, frequency
  corrections: what went wrong, how fixed

ChromaDB (semantic search)
  task_summaries, architecture_notes, conversation_log
```

When context compacts → RAG fills the gap. User never notices.

You can ask the Manager anything about your entire history:
- "Why did we use JWT?"
- "Where is the rate limiting logic?"
- "What would break if I changed the user schema?"

Instant answers. From memory. No re-running LLM.

---

## Permission Profile

Not OAuth scopes. Judgment rules.

```yaml
allowed:
  - read any file
  - write files in /src
  - run tests and linters
  - create branches

blocked:
  - push to main directly
  - delete files without confirmation
  - modify /payments or /auth alone
  - make external API calls

always_ask_me:
  - task scope changes significantly
  - blocked after 3 loops
  - anything irreversible

never_ask_me:
  - which library (use what's already there)
  - naming conventions (follow existing)
  - whether to add comments (don't)
```

Trust radius expands over time. Start narrow. Every correct autonomous decision earns more autonomy.

---

## Phases

### V1 — Prove the Core (4-6 weeks)

**Goal:** Does the supervision loop actually work?

```
IN
  Curl endpoint — task intake (no Telegram yet)
  Orchestrator asks ≤5 questions in terminal
  Claude Code runs headlessly
  Streaming events → Supervisor watches
  Permission layer enforced
  Interrupt/resume corrections
  Final quality check
  Completion summary printed

OUT
  Web UI, Telegram, RAG, learning layer, parallel workers
```

**Validation task:** Build a Hello World Android app in Kotlin.
This single task exercises every permission type, error recovery, build verification — everything the Supervisor needs to manage.

**V1 success:** User gives task, walks away, comes back to something that builds and runs — pinged 0 times.

**Concurrency:** V1 supports running multiple tasks in parallel. Each task gets its own:
- Working directory (`~/Desktop/supervisor-workspace/{task_id}/`)
- `.claude/settings.json` with the PreToolUse hook
- Claude Code subprocess
- Entry in the task state store, keyed by `task_id`

No shared mutable state across tasks. Practical ceiling ~5 concurrent tasks before Databricks rate limits or local CPU/RAM (Gradle builds) become the bottleneck. A real task queue is V2.

---

### V2 — Make It Smart (8-12 weeks)

```
IN
  Telegram bot — real interface
  Learning layer — skills.md auto-populated
  Full RAG — ChromaDB + SQLite
  ≤5 questions shrinks toward 0 for returning users
  Mid-task suggestions ("btw — should we add X?")
  Permission profile — set via Telegram conversation
  Basic web UI — task list, completion summaries

OUT
  Parallel workers, multi-task queue, fine-tuned model
```

**V2 success:** After 10 tasks, Orchestrator asks 0 questions for familiar task types.

---

### V3 — Scale the Intelligence (6+ months)

```
IN
  Parallel execution — fan-out/fan-in for complex tasks
  Multi-task queue with dependencies
  Full web UI — live graph, RAG chat interface
  Fine-tuned Orchestrator on real session data
  Any task type — code, research, writing, analysis
  Permission profile management UI
```

**V3 success:** The Orchestrator is measurably smarter than a generic Claude prompt. The fine-tuned model is the moat.

---

## The First Task Must Be Magic

The ≤5 questions must be surprisingly good — not "what stack?" but something that proves the Manager actually looked at the context and thought about implications.

The correction must be visible — completion summary shows what the Manager caught that Claude missed. This is the proof of value.

The first experience either earns trust permanently or destroys it forever.

---

## Positioning

> "A smart manager that never sleeps. Give it any task. Answer 5 questions. Come back when it's done."

Not a coding tool. Not a copilot. Not an agent runner.

**A manager.** The first AI product where you are genuinely not the supervisor.
