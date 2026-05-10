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

- For prose deliverables (blog posts, press releases, narrative essays), do NOT use markdown section headings unless the task explicitly asks for sectioned output. Default to flowing paragraphs.  _(applies_to: writing)_
  - **fix:** Re-read the task wording: if it says "blog post", "essay", "announcement", "narrative", treat it as flowing prose. Use headings ONLY when the task says "with sections" / "structured as" / "with headings". When in doubt, ask for clarification rather than defaulting to a sectioned product-explainer format.
- For writing tasks with a numeric length target (word count, char count, line count), run the COUNT command on the deliverable BEFORE claiming done and show the output in the action log. Stay within ±5% of the target.  _(applies_to: writing, research)_
  - **fix:** After writing, run `wc -w deliverable.md` (or `wc -c` / `wc -l` per the unit). If outside ±5%, edit and re-count. Show the FINAL count in the action log. Without this, the reviewer will flag missing verification AND/OR an off-target deliverable, costing 1-2 correction loops.
- If required inputs for a requested deliverable are unavailable or unverifiable, the executor must not silently reframe the task into an audit, gap analysis, or feasibility report unless that fallback was explicitly allowed.  _(applies_to: research, writing, data)_
  - **fix:** Have the executor stop and flag the unmet prerequisite, then either obtain approval for the fallback scope or continue until the original output can be completed as specified.
- When a task names exact entities to compare or transform, do not substitute adjacent versions, aliases, or currently served variants without explicitly resolving that identity mismatch against the requested names.  _(applies_to: research, data, writing)_
  - **fix:** Require the executor to prove each requested item maps to the exact source entity used in the deliverable, and if not, either find exact-match data or surface the mismatch as a blocker before calling the task done.
