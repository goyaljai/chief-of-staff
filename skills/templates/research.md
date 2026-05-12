# Research / Q&A template

Use this scaffold for "find me X", "compare A vs B", "summarize Y", "what are the top N…" tasks. The deliverable is a document, not a binary.

## Brief skeleton

- **Objective**: one sentence stating the question and the form of the answer.
- **Deliverable file(s)**: name a single markdown file (e.g. `<topic>_report.md`, `comparison.md`). Inline-only answers (chat) are valid — in that case set deliverables to `[]` and surface the answer in the executor summary.
- **What needs to be researched / produced**: 3-7 concrete sub-points the answer must cover. For "top N" tasks, list the N items by name if known or the criteria for selection.
- **Done / acceptance criteria** (numbered, verifiable):
  1. The markdown file exists with at least one section per requested item.
  2. Each claim is either cited (URL or source name) or clearly marked as opinion.
  3. The selection criteria are stated up-front so the user can argue with them.
- **Constraints**: word count? section format (bullets vs prose)? must include citations?
- **Quality bar**: prose-forward (G1 lesson — no markdown section headings unless the user asked for sections), substantive content (no one-line bullets pretending to be paragraphs).

## Common gotchas (encoded from prior tasks)

- Default to flowing prose paragraphs. Markdown headings should appear ONLY when the user explicitly asked for sectioned output.
- Don't pad with hedging language ("it's important to note..."). The user wants the substantive answer, not a meta-commentary on the answer.
- For "top N" tasks, the ordering matters — explain why each item earned its position, not just facts about it.
- Cite real, namable sources. If you can't find a real source, say so explicitly rather than fabricating one.
- For comparisons, structure the markdown so the comparison axis is parallel across items (same dimensions covered for each).

## Reviewer notes

- Deliverable is the single markdown file. Empty list `[]` is valid for inline-answer-only tasks.
- Reject reports that are <500 chars or lack any substantive content beyond restating the question.
- Reject markdown that uses headings when the user asked for prose ("blog post", "essay", "summary").
