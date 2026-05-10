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

_(Sorted by frequency across runs — patterns hit more often appear first. `[×N]` shows how many tasks have promoted this lesson.)_

- When the primary deliverable is a document, review against the artifact itself rather than relying on the executor's summary of what it contains.  _(applies_to: writing, research, ops)_
- **fix:** Request or inspect the full document text and verify each required section and claim directly against the brief before passing.
- When a task requires one deliverable to depend on another, brief and review for code-level reuse of the upstream logic rather than accepting duplicated behavior that merely produces the same output.  _(applies_to: code, data)_
- **fix:** Require the downstream artifact to import or call the upstream module/function directly and verify that dependency in the final artifact or run evidence.
