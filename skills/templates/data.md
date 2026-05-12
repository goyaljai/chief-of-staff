# Data template

Use this scaffold for data-shaped tasks — ETL, analysis, dataset generation, transformation pipelines, schema design.

## Brief skeleton

- **Objective**: one sentence stating the data transform / analysis + the shape of the output.
- **Deliverable file(s)**: name the output (`output.csv`, `analysis.md`, `pipeline.py`). For multi-file outputs (raw + processed + report), name all three explicitly.
- **What needs to be built / produced**:
  - Input schema (or "the user will paste the data").
  - Output schema (columns, types, ordering).
  - Transformation rules (joins, filters, aggregations).
  - Analysis questions to answer (if applicable).
- **Done / acceptance criteria** (numbered, verifiable):
  1. The output file(s) exist at the named path(s).
  2. Row count or shape matches expectation (e.g. "100 rows, 5 columns").
  3. A sample of N rows is shown in the executor summary so the user can sanity-check without opening the file.
  4. (For pipelines) `python3 pipeline.py < input.csv > output.csv` succeeds.
- **Constraints**: stdlib + pandas? specific output format? handle missing values how?
- **Quality bar**: deterministic output (same input → same output). If randomness is involved, seed it.

## Common gotchas (encoded from prior tasks)

- State the output schema BEFORE writing any transformation code. Half the bugs come from output drift.
- For CSV/Excel work, be explicit about delimiters, encoding, and header rows.
- For aggregations, state the GROUP BY axis up-front and the aggregate function (sum / mean / count / first / etc.).
- Validate output before declaring done — check row count, null distribution, and one sample row by eye.
- Floating-point comparisons need a tolerance, not equality. Use `pytest.approx` or explicit epsilon.

## Reviewer notes

- Deliverable is the output file(s). Source pipeline files (`.py`, `.sql`) are deliverables too if the user asked for a reusable pipeline; pure ad-hoc analysis means just the report.
- Reject outputs whose row count or shape disagrees with the brief.
