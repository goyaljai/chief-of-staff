# Escalation flow (G7+)

When the executor (Claude Code) hits something it can't decide alone,
it emits a structured marker. The supervisor parses it, persists the
parsed payload to `tasks.escalation`, interrupts the runner, and
pages the user via Telegram or the web UI.

## The structured format

The orchestrator brief instructs Claude to emit env walls in exactly
this shape (one block, at the top of an assistant message):

```
ESCALATION: <one-line summary>
WHY: <one paragraph explaining the wall>
OPTIONS:
A) <install / fix path>
B) <fallback path that still produces something useful>
ABORT) <description of cancelling>
```

All three markers (`ESCALATION:` + `WHY:` + `OPTIONS:`) MUST be
present for the parser to classify as `kind=environment`. This is a
deliberate tightening from audit r2 — pre-r2 the parser fired on any
prose line starting with `ESCALATION:`, including throwaway phrases
like *"I considered ESCALATION: but rejected it"*.

## Parser rules

`supervisor_loop._parse_escalation()`:

- `ESCALATION:` line at start of any line → `summary` field.
- `WHY:` block runs until the next OPTIONS / A) / B) line.
- `A)` / `B)` lines must have **whitespace after the delimiter** to
  match. Audit r2 fix: `^[\s\*\-]*A[\)\.](?:\s+|$)`. The previous
  pattern matched `A.I.` mid-prose and shadowed the real option.
- `ABORT)` line → `option_abort` field.
- Returns `kind=environment` only when ESCALATION + WHY + OPTIONS all
  present, else `kind=general` (fall through to legacy A/B handler).

## Auto-resolve (B classifier)

For environment escalations, the parser additionally classifies
`option_a` as `risk: low | high`:

- **Low risk** = single well-known package-manager install
  (`brew install`, `pip install`, `cargo install`, `rustup`,
  `npm i -g`, `nvm install`, `asdf install`, `pipx install`).
- **High risk** = anything sudo / multi-GB / license-prompted /
  long-running (Android SDK, Xcode CLT, raw `curl|sh`, `apt-get`,
  `softwareupdate`).

`auto_resolvable: true` is added to the escalation dict when risk is
low. The UI uses this to render a 🚀 "Auto-fix (low risk)" button.

When `COS_AUTO_RESOLVE_ESCALATIONS=1` is set, the supervisor would
auto-pick option_a without paging — currently NOT wired (deferred
pending more testing). For now `auto_resolvable=true` is just a
hint to the UI.

## Telegram surface

`_send_escalation` in `integrations/telegram/notifications.py`
renders the kind-specific keyboard:

- `kind=environment` → 3 buttons (A install / B fallback / Abort)
  with structured summary + why text.
- `kind=general` → legacy 2 buttons (A / B).

Tap on **Abort** → routes to `POST /task/{id}/cancel` (NOT
`/escalation`), so the runner subprocess is actually killed instead
of just recording the literal string `"abort"` as an answer. Audit
r2 fix.

After the user taps any button:
1. `STORE.answer_escalation(tid, answer)` records the answer + calls
   `_persist` (audit r3 fix — was in-memory only before).
2. `escalation_event.set()` wakes the supervisor's `_await_escalation`.
3. Supervisor builds a post-escalation prompt incorporating the
   answer and resumes via `claude --resume <session_id>`.

## Persistence

After audit r3 the escalation survives a server restart:

- `tasks.escalation` (jsonb) holds the parsed dict.
- `tasks.escalation_set_at` (double precision) holds the wall-time
  timestamp.
- `tasks.escalation_answer` (text) holds whatever the user tapped.

On hydrate, `_load_task_from_db` rehydrates all three; status
`escalated` is preserved (NOT marked `interrupted`) so the user's
button-tap flow keeps working post-restart.

## Lessons captured (today's audit)

- **r1**: ESCALATION mark in prose triggered fake env walls. Fix:
  require all 3 markers.
- **r2**: `option_a / option_b` regex matched `A.I.` mid-prose. Fix:
  require whitespace after delimiter.
- **r2**: Telegram Abort routed to /escalation (only recorded
  the answer). Fix: route to /cancel.
- **r3**: escalation was in-memory only. Fix: T6 column + T6b for
  escalation_answer.
- **r3**: ordering bug — `set_status("escalated")` triggered
  `_persist` BEFORE `state.escalation` was assigned. Same root cause
  as the result-ordering bug; both fixed.
