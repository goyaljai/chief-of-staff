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

- When requirements change mid-task, reviewers must resolve whether the new instruction supersedes the original brief before marking added work as scope drift or accepting the old deliverable as complete.  _(applies_to: code, data, ops, writing)_
  - **fix:** Pause and explicitly restate the current source of truth, then judge the work against that updated requirement set and require regeneration of any affected deliverables.
- When a required proof artifact is generated as a file (such as a screenshot, report, or export), completion is not proven unless the executor also surfaces that artifact through a visible listing or direct readback in the action log so reviewers can confirm it was actually created in the expected location.  _(applies_to: code, data, ops, writing)_
  - **fix:** After generating the artifact, immediately run a command like `ls -l` or equivalent on the exact path and, when feasible, expose the artifact itself in captured outputs.
- If the execution environment exposes only workspace artifacts at handoff, any required output created outside that artifact set must still be surfaced through a visible verification step or copied into the captured outputs before completion is claimed.  _(applies_to: code, data, ops, writing)_
  - **fix:** Confirm how deliverables will be captured, and if an external-path file may be omitted from artifacts, print it in the log or mirror it into the artifact set for verification.
- When a task requires writing to an external absolute path (for example under `/tmp`), completion is not proven unless the executor shows that exact file can be read back from disk after the write.  _(applies_to: code, data, ops, writing)_
  - **fix:** After writing, run a visible read/listing command on the exact path and include its contents in the action log before claiming done.
- When a task names exact entities to compare or transform, do not substitute adjacent versions, aliases, or currently served variants without explicitly resolving that identity mismatch against the requested names.  _(applies_to: research, data, writing)_
- If required inputs for a requested deliverable are unavailable or unverifiable, the executor must not silently reframe the task into an audit, gap analysis, or feasibility report unless that fallback was explicitly allowed.  _(applies_to: research, writing, data)_
- For writing tasks with a numeric length target (word count, char count, line count), run the COUNT command on the deliverable BEFORE claiming done and show the output in the action log. Stay within ±5% of the target.  _(applies_to: writing, research)_
- For prose deliverables (blog posts, press releases, narrative essays), do NOT use markdown section headings unless the task explicitly asks for sectioned output. Default to flowing paragraphs.  _(applies_to: writing)_
- When a task requires proving smooth distribution over time rather than just meeting a total quota, verification must include time-series evidence that checks spread or spacing, not only aggregate counts or final duration.  _(applies_to: code, data, ops)_
