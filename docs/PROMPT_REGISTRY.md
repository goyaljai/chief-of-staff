# Prompt registry

System prompts for each LLM persona, plus the per-family skill scaffolds.

| File | Used by | Purpose |
|---|---|---|
| `prompts/orchestrator.md` | `agents/orchestrator.Orchestrator.system` | The manager — meta-thinking, brief generation, escalation auto-resolve, correction prompts |
| `prompts/reviewer.md` | `agents/reviewer.Reviewer.system` | Independent QA — per-tool review, drift check, final review with G10 deliverables rules |
| `skills/global.md` | every orchestrator + reviewer prompt | Auto-promoted lessons, frequency-weighted, rendered from `skill_lessons` table |
| `skills/templates/{code,research,writing,data,ops}.md` | `Orchestrator.generate_skill_brief` | G5 — per-family scaffolds (brief skeleton, common gotchas, reviewer notes) |

## Cross-cutting rules baked into every prompt

- **Output JSON only** when the schema is defined. The reviewer's
  final_review specifically returns `{passed, summary, issues,
  next_steps, deliverables}` — `_extract_json` parses + repairs.
- **DELIVERABLE_PATHS marker** — orchestrator brief instructs Claude
  to end its final assistant message with `DELIVERABLE_PATHS: a, b, c`.
  Supervisor parses this in `_parse_executor_deliverable_marker`.
- **ESCALATION:/WHY:/OPTIONS:/ABORT) format** — orchestrator brief
  spells out the exact format for env walls. Supervisor's
  `_parse_escalation` requires all three markers (audit r2 fix —
  prevents prose mentions from triggering fake escalations).

## Future — prompt versioning (DOC3)

Today every push to `prompts/*.md` ships immediately. Phase 4 work to
A/B-roll prompt changes:

1. Tag each prompt with a `version` key in frontmatter.
2. Pin task → prompt version at task start; record in `tasks` row.
3. CI Promptfoo gate runs on the NEW version before merge.
4. Optional: shadow-mode comparison against prior version on a sample.

Currently deferred. Worth doing before C2 (merge skill+brief into one
call) lands, because that change is a meaningful prompt rewrite and
needs the safety rail.
