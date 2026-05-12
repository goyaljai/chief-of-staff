# Ops template

Use this scaffold for operational tasks — service config, deploy scripts, infra glue, monitoring setup, CI tweaks, runbooks.

## Brief skeleton

- **Objective**: one sentence stating the operational change + what was broken (or missing) before.
- **Deliverable file(s)**: name the config / script / runbook. Often more than one file — list each explicitly.
- **What needs to be changed / produced**:
  - The current state (what's there now).
  - The target state (what should be there after).
  - The migration / rollback plan (if applicable).
- **Done / acceptance criteria** (numbered, verifiable):
  1. The named files exist with the target content.
  2. A specific verification command succeeds (e.g. `kubectl apply --dry-run=server` or `terraform validate`).
  3. The change is REVERSIBLE — explicitly state the rollback command.
- **Constraints**: must not require downtime? secrets handling? backwards-compatible window?
- **Quality bar**: idempotent (re-running doesn't break things). All secrets via env vars or vault, never inline.

## Common gotchas (encoded from prior tasks)

- Ops changes are HIGH BLAST RADIUS. Always provide a rollback path in the brief, not just the forward path.
- Validate against a dry-run / lint command BEFORE declaring done. `kubectl apply --dry-run=server`, `terraform plan`, `gh actions lint`.
- Never paste real credentials into the deliverable. Use `${SECRET_NAME}` placeholders and document where they're sourced.
- For CI changes, run the new workflow on a sample PR before merging — most CI bugs only surface at runtime.
- For shell scripts: `set -euo pipefail` at the top, quote all expansions, validate inputs.

## Reviewer notes

- Deliverable is the changed config files + (for runbooks) the markdown.
- Exclude scaffolding the executor produced as a side-effect — only changed files are deliverables.
- Reject changes that lack a rollback section.
