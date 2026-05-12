"""Hand-labeled pairs for DSPy optimization (T3, $5-10 proxy budget).

Two datasets:
  - QUESTION_GEN: tests whether the orchestrator's question-gen
    short-circuits when a task is fully specified, and asks
    minimal-but-relevant Qs when it isn't.
  - REVIEWER: tests whether final_review correctly accepts/rejects
    deliverables and emits the right `deliverables` list (G10).

Why proxy and not full E2E:
  Full E2E means spinning up Claude executor subprocesses + a
  workspace per case ($50-200 per optimization run). The proxy here
  just checks the LLM's structured output against a labeled answer,
  which is cheap (~$0.03/call) and gives DSPy a real gradient on
  prompt quality. We accept that "model output looks right" is a
  weaker signal than "the executor actually produced a working APK"
  — the upside is we can iterate fast.
"""
from __future__ import annotations

import dspy


# ────────────────────────────────────────────────────────────────────
# Question-gen pairs.
# Each example is (task, answers_so_far_json) → expected outcome on
# `done` and `is_relevant_question`.
# ────────────────────────────────────────────────────────────────────
QUESTION_GEN: list[dspy.Example] = [
    # Fully specified — should short-circuit with done=true.
    dspy.Example(
        task=(
            "Write a single python file `fizzbuzz.py` in the workspace root. "
            "Print fizzbuzz 1-15. Use only the python stdlib. Verification: "
            "`python3 fizzbuzz.py` prints exactly 15 lines."
        ),
        answers_so_far="{}",
        expected_done=True,
        expected_max_questions=1,
    ).with_inputs("task", "answers_so_far"),

    dspy.Example(
        task=(
            "Write `hello.py` that prints 'Hello, world!' to stdout. Stdlib only. "
            "Acceptance: `python3 hello.py` prints exactly that line."
        ),
        answers_so_far="{}",
        expected_done=True,
        expected_max_questions=1,
    ).with_inputs("task", "answers_so_far"),

    # Vague — should ask one focused question, not three vague ones.
    dspy.Example(
        task="build a Python CLI calculator",
        answers_so_far="{}",
        expected_done=False,
        expected_max_questions=1,
        keywords_must_appear_in_question=[
            "operator", "operators", "parens", "parenthes", "filename",
            "deliver", "stdlib", "test", "verif",
        ],
    ).with_inputs("task", "answers_so_far"),

    dspy.Example(
        task="research a topic and tell me the answer",
        answers_so_far="{}",
        expected_done=False,
        expected_max_questions=1,
        keywords_must_appear_in_question=[
            "topic", "subject", "what", "depth", "format", "specific",
        ],
    ).with_inputs("task", "answers_so_far"),

    # Partially specified — already 3 answers given, next Q should be
    # done OR a confirmation, not a new dimension.
    dspy.Example(
        task="build a hello world Android app in Kotlin",
        answers_so_far=(
            '{"min_sdk":"24",'
            '"ui":"XML views",'
            '"done":"./gradlew assembleDebug succeeds"}'
        ),
        expected_done=True,
        expected_max_questions=1,
    ).with_inputs("task", "answers_so_far"),

    dspy.Example(
        task="write a 500-word blog post on fast iteration",
        answers_so_far=(
            '{"audience":"engineering managers",'
            '"tone":"opinionated, direct",'
            '"deliverable":"post.md"}'
        ),
        expected_done=True,
        expected_max_questions=1,
    ).with_inputs("task", "answers_so_far"),
]


# ────────────────────────────────────────────────────────────────────
# Reviewer pairs.
# Each example is (task, brief, workspace_listing, executor_summary)
# → expected (passed, deliverables_must_include, deliverables_must_exclude).
# ────────────────────────────────────────────────────────────────────
REVIEWER: list[dspy.Example] = [
    # Vacuous deliverable → fail.
    dspy.Example(
        task="build a Python CLI calculator that supports + - * /",
        brief='Build calc.py supporting + - * /. Verify: python3 calc.py "1+2" prints 3',
        workspace_listing="- calc.py (4 lines, prints \"TODO\")",
        executor_summary="I created calc.py with a TODO comment.",
        expected_passed=False,
    ).with_inputs("task", "brief", "workspace_listing", "executor_summary"),

    # Working deliverable → pass.
    dspy.Example(
        task="build a Python CLI calculator that supports + - * /",
        brief='Build calc.py supporting + - * /. Verify: python3 calc.py "1+2" prints 3',
        workspace_listing="- calc.py (45 lines, full recursive-descent parser, handles + - * / and parens)",
        executor_summary=(
            "Created calc.py with a Pratt parser supporting + - * / and parens. "
            "Verified: python3 calc.py \"1+2*3\" → 7, python3 calc.py \"(1+2)*3\" → 9."
        ),
        expected_passed=True,
        deliverables_must_include=["calc.py"],
    ).with_inputs("task", "brief", "workspace_listing", "executor_summary"),

    # Research → single doc.
    dspy.Example(
        task="research the 5 best mango varieties grown in India",
        brief=(
            "Research and produce a markdown report at `mango_report.md` "
            "listing 5 mango varieties with provenance + flavor notes."
        ),
        workspace_listing="- mango_report.md (3 KB, 5 varieties with sources)",
        executor_summary=(
            "Wrote mango_report.md. Listed Alphonso, Kesar, Dasheri, Langra, "
            "Chausa with regions + notes."
        ),
        expected_passed=True,
        deliverables_must_include=["mango_report"],
        deliverables_must_exclude=[],
    ).with_inputs("task", "brief", "workspace_listing", "executor_summary"),

    # Build task — APK is the deliverable, scaffolding is noise.
    dspy.Example(
        task="build a hello-world Android app",
        brief=(
            "Scaffold an Android app and produce app-debug.apk via "
            "`./gradlew assembleDebug`. Deliverable: the APK file."
        ),
        workspace_listing=(
            "- gradlew (script)\n"
            "- gradle/wrapper/...\n"
            "- app/build/outputs/apk/debug/app-debug.apk (3.4 MB)\n"
            "- settings.gradle.kts\n"
            "- build.gradle.kts"
        ),
        executor_summary=(
            "Scaffolded app, ran ./gradlew assembleDebug. APK at "
            "app/build/outputs/apk/debug/app-debug.apk."
        ),
        expected_passed=True,
        deliverables_must_include=["app-debug.apk"],
        deliverables_must_exclude=["gradlew", "settings.gradle", "build.gradle"],
    ).with_inputs("task", "brief", "workspace_listing", "executor_summary"),

    # User said "send me X" but X doesn't exist → fail.
    dspy.Example(
        task="send me a PDF version of the architecture doc",
        brief="Generate `architecture.pdf` from the prd_doc.md content.",
        workspace_listing="- prd_doc.md (12 KB)\n- notes.txt (1 KB)",
        executor_summary=(
            "I summarised the PRD inline in chat. Did not produce a PDF "
            "because reportlab wasn't installed."
        ),
        expected_passed=False,
    ).with_inputs("task", "brief", "workspace_listing", "executor_summary"),

    # Pure research / Q&A → empty deliverables list is valid.
    dspy.Example(
        task="explain the difference between a process and a thread in 4 sentences",
        brief="Answer the user's question inline. No file deliverable expected.",
        workspace_listing="(empty)",
        executor_summary=(
            "Explained: a process has its own address space; threads share "
            "the parent process's memory; processes are heavier; threads "
            "communicate via shared memory while processes use IPC."
        ),
        expected_passed=True,
        deliverables_must_include=[],
        deliverables_must_exclude=[],
    ).with_inputs("task", "brief", "workspace_listing", "executor_summary"),
]


def split(examples: list[dspy.Example]) -> tuple[list[dspy.Example], list[dspy.Example]]:
    """50/50 train/dev split. Tiny because the optimization is bounded
    by token budget, not data."""
    half = max(1, len(examples) // 2)
    return examples[:half], examples[half:]
