# Data flow — task lifecycle

End-to-end trace of a successful task, with timing + LLM-call points
flagged.

```
1. POST /task/run                                    [<10ms]
     ↓ creates TaskState, persists to tasks row, kicks off supervisor coroutine

2. think_and_ask (G9)                                [LLM 1-5x · ~2-15s total]
     ↓ adaptive Q&A loop. /task/questions returns one Q at a time.
     ↓ short-circuits on fully-specified prompts (done=true).

3. find_matching_skill                               [LLM 1x · ~2s]
     ↓ pgvector cosine search → Voyage rerank-2 → LLM classifies match quality

4. generate_skill_brief                              [LLM 1x · ~3-8s]
     ↓ writes SKILL.md (frontmatter + 7 sections per skill-creator format)
     ↓ G5 family templates injected here

5. env_audit (G8)                                    [parallel subprocesses · ~400ms]
     ↓ services/env_audit.audit() runs 18 probes from env_probes.json
     ↓ rendered as markdown block, fed to next phase

6. build_brief                                       [LLM 1x · ~3-8s]
     ↓ produces executor brief — Objective, Deliverables, Acceptance, Constraints
     ↓ optional ## Steps section if 2+ steps independent (DAG)
     ↓ DELIVERABLE_PATHS marker instruction included (audit r3)

7. parse_dag (regex)                                 [<1ms · NO LLM]
     ↓ extracts ## Steps block into [{id, action, depends_on, timeout_secs}]
     ↓ if None → sequential path; else → DAG path

8a. SEQUENTIAL path                                  [Claude 1x · varies]
       ↓ claude_runner.run() with brief as prompt
       ↓ every tool call → review_action (LLM, with C1 fast-path skip)
       ↓ B4 self_check fires every 3-4 unreviewed tools

8b. DAG path                                          [Claude N parallel · varies]
       ↓ workflows/dag/dag_executor.execute_dag()
       ↓ LangGraph map-reduce dispatcher
       ↓ PostgresSaver checkpoints every superstep (F6)
       ↓ each step is its own claude_runner subprocess

9. final_review                                      [LLM 1-3x · ~5-10s each]
     ↓ supervisor builds reviewer input:
        - full action log (last ~300 events)
        - workspace artifacts listing (binary deliverables surfaced)
        - brief-declared deliverables block (Approach A)
        - executor-declared DELIVERABLE_PATHS block (Approach B)
     ↓ reviewer returns {passed, summary, issues, next_steps, deliverables}
     ↓ if passed=False → correction loop (up to MAX_CORRECTION_LOOPS=3)

10. on done: write task.result, set status=done, persist          [DB write]
       ↓ _index_in_rag (pgvector + Mem0 fire-and-forget)
       ↓ _write_learning (find_promotable_lessons LLM 1x → skill_lessons UPSERT)

11. notification fan-out                             [<1s]
       ↓ Telegram: _send_artifacts uploads each deliverable file
       ↓ Web UI: SSE event stream
       ↓ status=done message with next_steps text
```

**Total LLM calls per successful sequential task: ~10–20** (think×1–5,
skill_brief×1, find_skill×1, build_brief×1, review_action×5–30 with
C1 cutting 60-70%, self_check×0–10, final_review×1, find_lessons×1).

**Median wall-time:** ~30–120 seconds for code tasks; ~10–30 seconds
for research/Q&A. Android builds (gradle wrapper download + assemble)
can hit 5–8 minutes.

## Failure paths

- **escalated** — Claude emits ESCALATION marker → supervisor parses,
  persists, interrupts runner, pages user. Status moves to
  `escalated`. Resumed via /task/{id}/escalation answer or /cancel
  for Abort.
- **interrupted** — server crash / SIGTERM / restart. Hydration
  marks active tasks as `interrupted`. Resume via /task/{id}/resume
  reads the LangGraph checkpoint + last good session_id and continues
  via `claude --resume`.
- **failed** — final_review.passed=False after MAX_CORRECTION_LOOPS, OR
  cost guardrail (PHK3) tripped, OR generate_skill / build_brief LLM
  errored, OR claude_runner subprocess died unrecoverably.
- **cancelled** — POST /task/{id}/cancel from user, or Abort button.
