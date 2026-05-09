# General — cross-domain quality patterns

Apply these to every task regardless of domain.

## Verification proof

A task is not done until **the verification step has been run AND its output is visible** in the action log.
- "I wrote the file" is not done.
- "It compiles" is not done if the goal needs it to *run*.
- "I ran the test" is not done if the test output isn't shown.

## Scope discipline

- Do not add libraries / dependencies / sections / files that the goal does not require.
- Do not introduce abstractions for hypothetical future needs.
- Do not refactor adjacent code unless explicitly in scope.

## No working around problems

Reject these as "done":
- Silencing errors instead of fixing them
- Weakening checks (assertions, lint, type, security) to pass
- Hardcoding values that should be derived
- Generating placeholder data that's not labeled as such
- Catching exceptions to swallow them
- Adding `# noqa`, `// eslint-disable`, `@SuppressWarnings`, etc., without justification

## Honest reporting

- The action log must contain the actual output, not a summary of it.
- "Build succeeded" claimed without the success line in the log = not proof.
- TodoWrite checkboxes mean nothing — they reflect Claude's own bookkeeping, not external truth.

## Learned from past runs

- When tool output is truncated, require the executor to rerun narrower or chunked commands that make the complete deliverable and each claimed check fully visible in the log.
- For small structured outputs, require line-by-line visible evidence of the entire artifact in the action stream rather than accepting file-write success plus a summarized verification claim.
- When the planned information source is unavailable due to auth, permission, or tool failure, immediately switch to a clearly defined fallback plan or pause explicitly for user input instead of spending turns probing the environment.
- Treat repeated off-target tool use as a process failure signal: after one correction, require the next actions to map directly to the task’s evidence needs or stop the run and re-brief.
- When a task depends on approximate or memory-based facts, explicitly brief for conservative phrasing rather than maximal specificity so the executor avoids unsupported precision.
- For ranked or subjective requests, define the evaluation lens in the brief (for example, fame, prestige, usability, cost, or performance) so item selection is consistent and reviewable.
