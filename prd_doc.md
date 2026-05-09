# chief-of-staff — Product Requirements Document

**Status as of 2026-05-09:** V1 shipped; V2 ~70% shipped (Telegram interface, dynamic skills, auto-promotion, retry/backoff, MCP hook coverage all live). V2.5 remaining items below.

Repo: https://github.com/goyaljai/chief-of-staff (public)

---

## The Problem

Knowledge workers and vibe coders using Claude Code spend most of their time managing Claude, not doing actual work. Claude asks questions mid-task. Claude says "done" when it isn't. Claude drifts from the original goal. The human is the bottleneck — not because they're slow, but because Claude constantly needs them.

Nobody has built the supervision layer. Every AI coding tool today assumes the human is the supervisor. We remove that assumption.

---

## The Product

**A smart manager that lives in Telegram, never sleeps, and never forgets.**

You give it any task — code, research, writing, analysis. It asks 3-5 questions upfront. Disappears. Manages Claude Code end to end. Comes back when it's done. Only interrupts you when it genuinely needs a human decision.

### The Three Layers

```
YOU (the Supervisor — the human boss)
↕ Telegram (only real decisions reach you)

ORCHESTRATOR (manager LLM, Databricks)
- Meta-thinks: generates a per-task SKILL.md (Anthropic skill-creator format)
- Asks 3-5 sharp clarifying questions
- Builds the executor brief
- Builds correction prompts when reviewer fails
- Auto-resolves stuck escalations after 30 min using full context

REVIEWER (independent QA LLM, Databricks)
- Sees only goal + action stream + workspace artifacts
- Blind to the orchestrator's brief — judgment can't be biased
- Per-action review on Bash/Write/Edit/MultiEdit/risky MCP tools
- Final review reads actual workspace files (ground truth)
- approve / correct / escalate

CLAUDE CODE (executor, headless via CLI)
- Does the actual work
- Doesn't know it's being reviewed
- Gets corrected via --resume + correction prompt
- Never talks to you directly

PRE-TOOL-USE HOOK (real OS-level enforcement)
- Blocks catastrophic ops (rm -rf /, dd to disk, shutdown)
- Blocks destructive ops outside the task workspace
- Blocks writes to absolute paths outside workspace (except /tmp, /var/tmp)
- Logs sudo, curl|bash, etc. for reviewer
- Covers Bash/Write/Edit/MultiEdit + MCP variants
```

### The Escalation Chain

```
Claude Code does something ambiguous / Reviewer can't decide
        ↓
Orchestrator builds correction prompt → Claude resumes (loops 1, 2, 3)
        ↓
Still stuck after 3 loops → Reviewer escalates to you on Telegram (A/B question)
        ↓
You reply A or B → Orchestrator feeds answer to Claude → resumes
        ↓
You don't reply in 30 min → Orchestrator auto-decides best option from full context
```

You only get pinged on truly stuck moments. Auto-resolve protects against you being unreachable.

---

## What Makes It Different

| Product | Has supervisor layer? | Real permission enforcement? | Minimal interruption? | Open source? |
|---|---|---|---|---|
| Devin | No — you are the supervisor | No | Partially | No (closed, $500/mo) |
| OpenHands | No — autonomous loop, you watch | No | No | Yes |
| Cursor / Copilot | No — approves every step | No | No | No |
| LangGraph / CrewAI | DIY | DIY | DIY | Yes (frameworks, not products) |
| **chief-of-staff** | **Yes — as a product** | **Yes — PreToolUse hook** | **Yes — Telegram, 3-5 Qs** | **Yes (MIT)** |

**Core insight:** The Supervisor's value is *management instinct*, not *domain knowledge*. The orchestrator generates a fresh SKILL.md per task (Anthropic skill-creator pattern: YAML frontmatter + body) that captures what *this* task needs — failure patterns, verification criteria, scope boundaries. Same machine handles a code task, a research task, a writing task — orchestrator figures out what each needs.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  USER (phone — Telegram app)                                     │
└──────────────────────────────────────────────────────────────────┘
                            ↕ HTTPS to api.telegram.org
┌──────────────────────────────────────────────────────────────────┐
│  TELEGRAM BOT (telegram_bot.py)                                  │
│  - long-polling Telegram                                         │
│  - whitelist on user_id                                          │
│  - conversation handler (task → questions → answers)             │
└──────────────────────────────────────────────────────────────────┘
                            ↕ http://localhost:8000
┌──────────────────────────────────────────────────────────────────┐
│  FASTAPI SERVER (main.py)                                        │
│  - POST /task/questions   → meta-think + 3-5 Qs                  │
│  - POST /task/run         → start supervised task                │
│  - GET  /task/{id}        → live state                           │
│  - POST /task/{id}/escalation → answer A/B                       │
│  - in-memory skill_preview cache (10 min TTL)                    │
└──────────────────────────────────────────────────────────────────┘
                            ↕
┌──────────────────────────────────────────────────────────────────┐
│  SUPERVISOR LOOP (supervisor_loop.py)                            │
│   1. Generate per-task SKILL.md → workspace/skills/SKILL.md      │
│   2. Build brief from task + clarifications + SKILL.md           │
│   3. Spawn Claude Code (claude_runner.py)                        │
│   4. Stream events → per-action reviewer (where applicable)      │
│   5. Final review reads workspace artifacts (ground truth)       │
│   6. If issues → correction loop (max 3) via --resume            │
│   7. If stuck after 3 → escalate to user                         │
│   8. After done/failed → promote generic lessons to global.md    │
└──────────────────────────────────────────────────────────────────┘
                            ↕
┌──────────────────────────────────────────────────────────────────┐
│  ORCHESTRATOR + REVIEWER (orchestrator.py, Databricks LLM)       │
│  - think_and_ask(task) → skill_preview + questions               │
│  - generate_skill_brief(task, answers, preview) → SKILL.md       │
│  - build_brief(task, answers, workspace) → executor brief        │
│  - review_action(action, recent) → approve/correct/escalate      │
│  - final_review(goal, log + artifacts, workspace) → pass/issues  │
│  - find_promotable_lessons(...) → list[lesson]                   │
│  - auto_resolve_escalation(...) → A/B                            │
│  Retry: 5 SDK retries + 3 explicit transient retries (exp.bo.)   │
└──────────────────────────────────────────────────────────────────┘
                            ↕ subprocess
┌──────────────────────────────────────────────────────────────────┐
│  CLAUDE CODE CLI (headless, --output-format stream-json)         │
│  - per-task workspace: ~/Desktop/supervisor-workspace/{task_id}/ │
│  - per-task hook in {workspace}/.claude/settings.json            │
│  - 20-min loop timeout                                           │
└──────────────────────────────────────────────────────────────────┘
                            ↕ PreToolUse fires
┌──────────────────────────────────────────────────────────────────┐
│  PERMISSION HOOK (permission_hook.py, separate process)          │
│  - Reads tool call from stdin (Claude Code spec)                 │
│  - Workspace boundary check (catastrophic + scoped)              │
│  - Allow / allow_unknown / review / block (exit codes 0 or 2)    │
│  - Per-task hook log                                             │
│  - Covers Bash, Write, Edit, MultiEdit, mcp__.*                  │
└──────────────────────────────────────────────────────────────────┘
```

### Tech Stack

| Layer | Technology | Status |
|---|---|---|
| Orchestrator + Reviewer LLM | Databricks AI Gateway (OpenAI-compatible, model `databricks-gpt-5-4`). Future: fine-tuned model on real session data — that's the moat. | ✅ shipped |
| Claude Code execution | Claude Code CLI headless + `--output-format stream-json --verbose` | ✅ shipped |
| Permission enforcement | PreToolUse hook script (per-task `<workspace>/.claude/settings.json`) | ✅ shipped |
| Correction mechanism | `interrupt()` + resume with session_id + correction prompt | ✅ shipped |
| Orchestration | Plain async/await + asyncio (no LangGraph — overkill for V2 scale) | ✅ shipped |
| Telegram | python-telegram-bot 22.x, long polling | ✅ shipped |
| Per-task skill format | Anthropic skill-creator (YAML frontmatter `name`/`description` + body) | ✅ shipped |
| Skill auto-promotion | After every task: orchestrator extracts generic lessons → appended to `skills/global.md` | ✅ shipped |
| Databricks robustness | OpenAI SDK `max_retries=5` + explicit transient-error backoff (3 retries, 4-8-16s) | ✅ shipped |
| Per-loop timeout | 20-minute cap on Claude Code subprocess | ✅ shipped |
| Structured memory (SQLite) | not yet | ⏳ V2.5 |
| Semantic memory (ChromaDB) | not yet | ⏳ V2.5 |
| Skill library + match-by-description | dynamic per-task SKILL.md works; library reuse by description = next | ⏳ V2.5 |
| SSE / live streaming | poll-based for now | ⏳ V2.5 |
| Cost tracking | not yet | ⏳ V2.5 |
| Web UI | not yet | ⏳ V3 |

### Key Technical Validations (all confirmed by E2E tests)

- Claude Code runs headlessly via CLI ✅
- Streaming events give Reviewer full visibility ✅
- `--resume` retains full context across correction loops ✅
- Reviewer reads workspace artifacts as ground truth (avoids false-fail from log truncation) ✅
- PreToolUse hook = real OS-level enforcement (workspace boundary checks pass and fail correctly) ✅
- Skill auto-promotion: research task ran end-to-end, 2 generic lessons promoted to `global.md` ✅
- Telegram bot conversation flow: task → 3-5 Qs → answers → run → result, all via long polling ✅
- E2E validation: Hello World Android (built APK in 1 loop, ~9 min) + research task (5 mango varieties, 2 loops, completed) ✅

---

## The Skill Model (V2)

**Per-task skill: dynamic, not pre-baked.**

```
~/Desktop/supervisor-workspace/{task_id}/skills/SKILL.md

---
name: <kebab-case-id-derived-from-task>
description: Use this skill whenever the user asks to <X>...
              (Anthropic skill-creator "pushy" trigger phrasing)
---

# <Title>

## Objective
## What 'done' means
## Knowledge / standards that apply
## Failure patterns to watch for
## Verification required
## Gotchas
## Scope boundaries
```

**The lifecycle:**

```
1. New task arrives
   orchestrator.think_and_ask(task)
     → meta-thinks privately (skill_preview)
     → asks 3-5 sharp questions grounded in that thinking

2. User answers (via Telegram or curl)
   orchestrator.generate_skill_brief(task, answers, skill_preview)
     → refines preview into final SKILL.md
     → saved to workspace/skills/SKILL.md

3. Task runs
   orchestrator + reviewer load:
     skills/global.md  (universal patterns)
     workspace/skills/SKILL.md  (this task's playbook)

4. Task completes
   orchestrator.find_promotable_lessons(task, SKILL.md, log, review)
     → identifies lessons GENERAL enough for FUTURE different tasks
     → appended to skills/global.md (deduped, capped at 50)

5. Future task
   global.md is richer. Per-task SKILL.md still generated fresh.
   (V2.5: also retrieve similar past SKILL.mds via description match.)
```

**Why this design:**
- No keyword-based domain detection (the V1 mistake)
- No hardcoded `android.md` / `research.md`
- The orchestrator decides what each task needs, fresh
- Cold start: global.md empty → only meta-thinking. After 50 tasks: global.md is rich, picks up the slack.
- Skill files become a RAG corpus (V3): "what did we learn about X" → vector-search past SKILL.mds.

---

## Permission Profile

Not OAuth scopes. Not domain rules. **Workspace boundaries + judgment.**

**Hard-blocked (catastrophic, no legitimate dev use):**
- `rm -rf /`, `rm -rf ~`, `rm -rf $HOME`, `rm -rf /*`
- `dd if=...of=/dev/sd*`, `mkfs.* /dev/...`
- `shutdown`, `reboot`, `halt`, `poweroff`, `diskutil erase`

**Workspace boundary (NEW in V2):**
- Destructive ops (`rm`, `rmdir`, `shred`, `trash`) targeting absolute paths *outside* the task workspace → BLOCKED
- `Write`/`Edit`/`MultiEdit` to absolute paths *outside* workspace → BLOCKED
- Exception: `/tmp`, `/var/tmp`, `/private/tmp`, `/private/var/folders` are safe-outside (Gradle, scratch space)

**Flagged for reviewer (allowed but logged):**
- `sudo`, `su`, `curl|bash`, `wget|bash`, `chmod 777`, `eval`, `pip install -<flag>`, `npm install -g`
- Unknown commands not on the safelist

**Always allowed (safelisted dev tools):**
- `gradle/gradlew`, `java/javac/kotlin/kotlinc`
- `python/pip`, `node/npm/npx/yarn/pnpm`, `go`, `cargo`
- `git`, `ls`, `cat`, `mkdir`, `cp`, `mv`, `find`, `grep`, etc.

Hook is per-task in `<workspace>/.claude/settings.json`. Your normal Claude Code usage outside the supervised loop is **untouched**.

---

## Phases

### V1 — Prove the Core ✅ DONE

**Goal:** Does the supervision loop actually work?

**Shipped:**
- Curl endpoint — task intake (no Telegram)
- Orchestrator asks 3-5 questions in terminal
- Claude Code runs headlessly with `--output-format stream-json --verbose`
- Streaming events → Supervisor Loop watches every action
- PreToolUse hook = real enforcement (per-task `.claude/settings.json`)
- Workspace boundary checks — `rm -rf /Users/jai.goyal/Documents/...` is blocked
- Interrupt + resume corrections (3 max)
- Independent reviewer — sees only goal + action stream
- Auto-resolve escalation after 30 min
- Concurrency: ~5 parallel tasks
- Per-task workspace `~/Desktop/supervisor-workspace/{task_id}/`

**Validation:** Hello World Android built end-to-end in 1 loop, 8m52s. APK was a real APK with valid `classes.dex` + `AndroidManifest.xml`. Reviewer caught real Android failure patterns (FAIL_ON_PROJECT_REPOS downgrade, hardcoded `local.properties`).

---

### V2 — Make It Smart 🟡 ~70% DONE

**Goal:** Make the orchestrator measurably smarter. Make the interface real.

**Shipped:**
- ✅ **Telegram bot** — long-polling, single-user whitelist, conversation flow
- ✅ **Dynamic skill model** — per-task SKILL.md generated from meta-thinking (Anthropic skill-creator format)
- ✅ **skills/global.md auto-promotion** — generic lessons promoted after each task
- ✅ **Reviewer reads workspace artifacts** — ground truth at final review (fixed the truncation false-fail bug)
- ✅ **Per-action reviewer expanded** — covers Write/Edit/MultiEdit/MCP variants, not just Bash
- ✅ **MCP hook coverage** — `mcp__.*` matcher in `settings.json`; hook recognizes MCP bash/write variants
- ✅ **Databricks retry/backoff** — 5 SDK retries + 3 explicit transient retries (exp. backoff)
- ✅ **Per-loop timeout** — 20-min cap on Claude Code subprocess
- ✅ **skill_preview reuse** — cached server-side (10-min TTL), saves 1 LLM call per task
- ✅ **Existing-repo support** — `working_dir` param in /task/run

**Outstanding (V2.5):**
- ⏳ **Skill library / match-by-description** — when new task arrives, retrieve similar past SKILL.md by description before regenerating. This is what closes the loop on "asks 0 questions after 10 tasks of same type".
- ⏳ **RAG layer (SQLite + ChromaDB)** — permanent memory across context compaction. Vector search over past skills, decisions, corrections.
- ⏳ **SSE streaming** — `/task/{id}/stream` endpoint for live events instead of polling
- ⏳ **Cost tracking** — Databricks tokens + Claude tokens, $ per task
- ⏳ **Cancel endpoint** — POST /task/{id}/cancel
- ⏳ **Workspace TTL/cleanup** — disk fills up otherwise
- ⏳ **Mid-task suggestions** — "btw, should we also add X?"
- ⏳ **Permission profile via Telegram conversation** — set custom rules per task type
- ⏳ **Web UI** — task list + completion summaries

**V2 success criteria:**
- ✅ Telegram works end-to-end (DONE)
- ✅ Skills auto-evolve (DONE)
- ⏳ Asks 0 questions for familiar task types (needs skill library — V2.5)
- ✅ Reviewer catches real-world failure patterns (DONE)

---

### V3 — Scale the Intelligence

**Goal:** Move from "smart assistant for me" to "platform-grade product".

**In:**
- Parallel execution — fan-out/fan-in for compound tasks
- Multi-task queue with dependencies
- Full web UI — live action graph, RAG chat ("ask me anything about my history")
- Native iOS/Android apps (after Telegram validates the use pattern)
- **Fine-tuned Orchestrator** on real session data — *this is the moat*
- Multi-user with proper auth, billing, isolation
- 24/7 hosted backend (deferred from V1; GCP VM available)

**V3 success:** Fine-tuned orchestrator measurably outperforms generic Claude prompts on the same task. We have a defensible dataset.

---

## The First Task Must Be Magic

The 3-5 questions must be surprisingly good — not "what stack?" but something that proves the Manager actually looked at the context and thought about implications.

The correction must be visible — completion summary shows what the Manager caught that Claude missed. This is the proof of value.

The first experience either earns trust permanently or destroys it forever.

**V2 validation runs:**
- Hello World Android: passed in 1 loop. APK valid. ✅
- "Tell me good food joints in BTM": passed in 2 loops, with strict reviewer catching first-loop weakness. ✅
- 5 best mango varieties: passed in 2 loops; reviewer rejected first attempt for missing visible verification, accepted second. ✅
- Skills auto-promoted real lessons: *"For ranked or subjective requests, define the evaluation lens in the brief"*. ✅

---

## Positioning

> "A chief of staff for your AI. You delegate. It manages. Only escalates when truly stuck."

Not a coding tool. Not a copilot. Not an agent runner.

**A manager.** The first AI product where you are genuinely not the supervisor.

Repo: [github.com/goyaljai/chief-of-staff](https://github.com/goyaljai/chief-of-staff)
