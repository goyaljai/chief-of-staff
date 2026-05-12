# Code style

## Comments

**Default to writing none.** Add a comment only when the WHY is
non-obvious — a hidden constraint, a subtle invariant, a workaround
for a specific bug, behavior that would surprise a reader.

If removing the comment wouldn't confuse a future reader, don't write it.

**Don't explain WHAT the code does** — well-named identifiers already
do that. Don't reference the current task, fix, or callers
("used by X", "added for the Y flow", "handles the case from issue
#123") — those belong in the PR description and rot as the codebase
evolves.

**Module docstrings carry design rationale.** When a file has a
non-obvious choice (`services/env_audit.py` is data-driven; `services/memory.py`
fire-and-forgets the Mem0 add), the module-level docstring explains
why. The decay rate is much lower than inline comments because the
docstring is right next to the file's purpose.

## Lessons embedded inline

When fixing a real production bug, leave a short reference like
`# Bug fix (Phase 3 audit r3): <one line>`. The intent is breadcrumbs
for the next person who lands here, not a changelog dump. Keep them
to one or two lines and link out to the relevant docs/ file when more
detail is warranted.

## Imports

Top of file. Sorted: stdlib, third-party, local. No `from X import *`.

## Type hints

Required on public function signatures. Optional on locals where the
type is obvious. We're on Python 3.12 — use `int | None`, not
`Optional[int]`.

## Logging

Use `logging.getLogger(__name__)`. No `print()` in production code paths
(except CLI scripts with explicit user-facing output).

## Exceptions

- Catch the narrowest exception class that makes sense.
- Don't catch `Exception` to swallow errors silently. If a path is
  designed to be best-effort (e.g. Mem0 add fire-and-forget), log
  the failure with `log.warning` and `continue`.

## Tests

Every module that holds load-bearing logic should have a corresponding
`eval/test_*.py`. The test file's docstring explains what regression
it's preventing. CI runs them all on every PR.

## Migrations

See [PERSISTENCE.md](PERSISTENCE.md). The 5-step procedure is enforced
by `eval/test_schema_drift.py`.
