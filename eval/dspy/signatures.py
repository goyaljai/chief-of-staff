"""DSPy signatures wrapping the orchestrator's question-gen and the
reviewer's final_review (T3).

Why DSPy modules and not raw prompts:
  DSPy's optimizers (BootstrapFewShot, MIPROv2) treat the
  *signature* as an opaque program and improve its *demonstrations*
  + system instructions. We don't care that the prod orchestrator
  reads its prompt from a markdown file — what matters is that we
  can express the same shape (task → JSON answer) as a DSPy
  module so the optimizer has something to mutate.

After optimization, ``optimize.py`` exports the compiled program's
final demos + instructions to JSON, which a follow-up commit can
drop into ``prompts/orchestrator.md`` / ``prompts/reviewer.md`` as
hand-curated few-shots. That gives prod the win without coupling
prod to DSPy at runtime.
"""
from __future__ import annotations

import dspy


class QuestionGenSig(dspy.Signature):
    """Decide whether to ask ONE more clarifying question, given a
    task and any prior answers.

    Output rules:
      - `done` is true iff the task is fully specified OR you have
        already asked 5 questions.
      - `next_question` is the single next question to ask, or empty
        when done.
      - Each question must probe a real ambiguity (deliverable filename,
        runtime, scope, output format). Do NOT generate filler Q4/Q5.
    """
    task: str = dspy.InputField(desc="The user's high-level task")
    answers_so_far: str = dspy.InputField(
        desc="JSON map of {prior_question: answer}; '{}' if none",
    )
    next_question: str = dspy.OutputField(
        desc="The single next question to ask, or '' if done",
    )
    done: bool = dspy.OutputField(
        desc="True iff no more questions are needed or 5 already asked",
    )


class ReviewerSig(dspy.Signature):
    """Independent QA review of an executor's deliverable.

    You did NOT see the brief — only the final workspace state and
    the executor's own summary. Decide whether the deliverable meets
    the user's task and declare which workspace files are the
    user-facing deliverables (G10).

    Deliverables rules:
      - Include only files that fulfil the user's stated task.
      - Exclude scaffolding (gradlew, build.gradle*, settings.gradle*,
        package-lock.json, node_modules, *.pyc, __pycache__, .gitignore).
      - For research / Q&A tasks where the deliverable is a single
        document, list that one path.
      - For build tasks where the deliverable is a binary, list the
        binary itself, not the source files used to produce it.
      - Empty list is valid for pure-research / inline-answer tasks.
      - If the user said "send me X" and X doesn't exist, set
        passed=false and deliverables=[].
    """
    task: str = dspy.InputField(desc="The user's high-level task")
    brief: str = dspy.InputField(desc="The brief that was handed to the executor")
    workspace_listing: str = dspy.InputField(
        desc="Final workspace state — relative paths + sizes / notes",
    )
    executor_summary: str = dspy.InputField(
        desc="The executor's own summary of what they produced",
    )
    passed: bool = dspy.OutputField(desc="True iff the deliverable meets the user's task")
    deliverables: list[str] = dspy.OutputField(
        desc="Workspace-relative paths the user explicitly cares about",
    )
