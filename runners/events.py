"""ClaudeEvent + TaskResult dataclasses.

These are the data contracts the rest of the system speaks to a Claude
Code subprocess in:

  ClaudeEvent  — one line of `claude --output-format stream-json` parsed
                 into a typed event. Streamed live from runner.run() so
                 the supervisor + reviewer + dashboard can observe Claude
                 mid-task. The `raw` dict is the original JSON in case a
                 caller needs a field we don't model.

  TaskResult   — what runner.run() returns at the end. `success` is the
                 subprocess returncode==0 check; `events` is the full
                 sequence (some callers want to replay it); `output`
                 concatenates the result-event text fields for convenience.

The `cost_usd` field is set when the result event includes a `cost_usd`
key (older Claude CLIs include it inline). New CLIs may stop emitting
it — the field stays `None` in that case.
"""
from dataclasses import dataclass, field


@dataclass
class ClaudeEvent:
    type: str
    tool_name: str | None = None
    tool_input: dict = field(default_factory=dict)
    tool_output: str | None = None
    text: str | None = None
    session_id: str | None = None
    is_error: bool = False
    raw: dict = field(default_factory=dict)


@dataclass
class TaskResult:
    success: bool
    session_id: str | None
    output: str
    events: list[ClaudeEvent]
    cost_usd: float | None = None
