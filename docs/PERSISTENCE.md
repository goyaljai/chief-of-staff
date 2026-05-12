# Persistence

Every `TaskState` field that matters across a server restart MUST have:
1. A column on the `tasks` table
2. A slot in the `INSERT INTO tasks (...)` clause of `persistence/tasks_repo.upsert_task`
3. A field in the hydration code in `persistence/store._load_task_from_db`

If any one of those three is missing, the field silently falls off
on a server crash. The drift-check eval test (`eval/test_schema_drift.py`)
fails CI when these three lists disagree, so today's escalation-persistence
regression stays a one-time event.

## Runtime-only fields (intentionally NOT persisted)

These are excluded from the drift check on purpose:

| Field | Why |
|---|---|
| `escalation_event` | An `asyncio.Event` — process-local primitive, not serializable. |
| `log` | Lives in the separate `log_entries` table, not on the `tasks` row. |

Anything else added to `TaskState` is presumed persistable.

## Lessons captured (today's audit)

### Audit r3 — escalation column gap (T6)
Pre-T6 the supervisor's `state.escalation` was a `dict | None` field on
`TaskState` but had no column on `tasks`, no slot in `upsert_task`, no
hydration. A server crash mid-escalation lost the parsed
ESCALATION/WHY/OPTIONS payload — the user's inline buttons in Telegram
or the web UI had nothing to bind to after a restart. Fix: migration
`postgres_v2_0_t6_escalation.sql` added `escalation jsonb` and
`escalation_set_at double precision`.

### Audit r3 — runtime state gap (T6b)
Same pattern caught five more fields: `user_notes`, `corrections`,
`escalation_answer`, `claude_plan`, `skill_preview`. The user-visible
ones were `user_notes` (POST /task/{id}/note dropped silently on
restart) and `escalation_answer` (race window between user-tap and
supervisor-resume). Migration `postgres_v2_0_t6b_persist_runtime_state.sql`.

### Audit r3 — ordering bug
`STORE.set_status(...,"done")` calls `_persist`. Three sites in
`supervisor_loop.py` assigned `self.task.result = {...}` AFTER
`set_status`, so the DB row kept `result=NULL` and post-restart
hydration lost the deliverables list. Fix: assign result first, then
set_status. Always.

### Audit r3 — answer_escalation
`STORE.answer_escalation` mutated `state.escalation_answer` but
didn't call `_persist`. A crash between the user's inline-button tap
and the supervisor's resume would lose the answer silently. Fix:
`_persist(s)` after the mutation.

## How to add a new TaskState field

1. Add the field to `TaskState` in `persistence/store.py`.
2. Add a migration `migrations/postgres_v2_0_t<N>_<name>.sql` with
   `ALTER TABLE tasks ADD COLUMN IF NOT EXISTS <name> <type>;`.
3. Add the column to the `INSERT INTO tasks (...)` clause AND the
   `ON CONFLICT ... DO UPDATE SET` clause in `tasks_repo.upsert_task`,
   AND the value tuple. Three places in one function.
4. Add the field to the hydration block in `store._load_task_from_db`.
5. Run `python3 eval/test_schema_drift.py` locally — should print PASS.
6. CI re-runs the drift check on every PR.

If the field is process-local (asyncio primitive, log), add it to
`_RUNTIME_ONLY_FIELDS` in the drift-check test.
