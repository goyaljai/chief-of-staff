"""Reviewer — independent QA LLM role.

The reviewer is deliberately blind to the orchestrator's brief. It sees:
  • the user's goal
  • the action log (what Claude actually did)
  • the workspace artifacts (what Claude actually produced)
  • the SKILL.md (the public plan)

It does NOT see the orchestrator's executor brief. The blindness is the
whole point — if the reviewer reads the brief, they grade against the
brief instead of against the goal, which lets a brief-following but
goal-missing run pass review. Two phases:

  review_action  — per-action sanity check. Returns {decision, message}.
                   Used by the supervisor mid-run to flag drift early.
  final_review   — full pass/fail at end of a loop. Returns
                   {passed, summary, issues, next_steps}. The
                   supervisor uses `passed=true AND issues==[]` as the
                   one-bit pass signal.
"""
import json

from .llm import _chat, _extract_json
from .prompts import _load_prompt, _load_skills


# C1 (Phase 4 cost reduction): tools/inputs that match these patterns
# are pre-approved without an LLM review_action call. The LLM call
# costs ~$0.01-0.03 each and a typical task fires 5-30 of them; for
# an obviously-safe `pytest tests/` or `git status` we don't need the
# LLM to tell us "approve". The supervisor still snapshots Write/Edit
# pre-mutation (D1 undo) and the permission_hook still blocks the
# dangerous patterns at the OS level — this fast-path only skips the
# LLM advisory layer.
_REVIEW_FAST_PATH_BASH_PREFIXES = (
    "ls", "pwd", "cat", "echo", "head", "tail", "wc",
    "find", "grep", "rg", "tree", "file", "stat",
    "which", "type", "command", "date", "sleep", "env",
    "git status", "git log", "git diff", "git show", "git branch",
    "git stash list", "git remote", "git config --get",
    "pytest", "unittest", "jest", "vitest", "mocha", "tox",
    "python3 -m pytest", "python -m pytest",
    "npm test", "npm run test", "yarn test", "pnpm test",
    "./gradlew test", "./gradlew lint", "./gradlew check",
    "node --version", "python3 --version", "go version",
)
# Static fast-path REMOVED post-cost-leak-audit. The pattern-matching
# approach was hiding cost rather than measuring it. We'll redesign a
# proper first-class skip mechanism — likely an LLM-classifier-once
# with cache, or task-family-aware safelist — after we have several
# real-task traces to inform the design.
# The _REVIEW_FAST_PATH_BASH_PREFIXES tuple is kept for reference
# only; it informs the future design but no code reads it today.


class Reviewer:
    """Independent QA reviewer — sees only goal + action log + Skill.md.
    Blind to the brief."""

    def __init__(self):
        self.system = _load_prompt("reviewer")

    def review_action(
        self,
        goal: str,
        tool_name: str,
        tool_input: dict,
        tool_output: str | None = None,
        recent_actions: str = "",
        workspace: str | None = None,
        inline_skill: str = "",
    ) -> dict:
        # C1 fast-path REMOVED. Per cost-leak audit, the static-pattern
        # fast-path was hiding a leak (ContextVar usage_callback didn't
        # propagate to the executor thread, so fast-pathed AND non-
        # fast-pathed review_action calls were both invisible to
        # telemetry). Every review_action now hits the LLM so the
        # llm_call event captures real cost data. We'll redesign a
        # proper first-class skip mechanism once we have several
        # real-task traces to inform the design.
        skills = _load_skills(workspace=workspace, inline_skill=inline_skill)
        user = (
            f"Goal: {goal}\n\n"
            f"Recent actions:\n{recent_actions}\n\n"
            f"Action under review:\n"
            f"  Tool: {tool_name}\n"
            f"  Input: {json.dumps(tool_input)[:500]}\n"
            f"  Output: {(tool_output or '')[:500]}\n\n"
            'Phase: per-action review. Output JSON only with shape '
            '{"decision":"approve|correct|escalate|request_evidence","message":"<short>"}.'
        )
        # P2 #6: skills_context dropped from review_action — reviewer
        # doesn't need the lesson library to approve `ls`. Saves ~900
        # tokens × every review_action call.
        raw = _chat(self.system, user, max_tokens=512)
        data = _extract_json(raw)
        # P0 #19: parse failure must NOT silently approve. Pre-fix, an
        # empty {} from _extract_json would default to decision=approve,
        # so a malformed LLM response = silent free pass through the
        # gate (we paid for the call but got no advisory). Now treat
        # missing/unrecognized decision as request_evidence so the
        # supervisor logs it loudly and re-asks instead of silently
        # passing the action.
        decision = data.get("decision")
        if decision not in ("approve", "correct", "escalate", "request_evidence"):
            return {
                "decision": "request_evidence",
                "message": "[reviewer parse failure] response not parseable as expected JSON",
                "_parse_failure": True,
                "_raw_excerpt": (raw or "")[:200],
            }
        return {"decision": decision, "message": data.get("message", "")}

    def drift_check(
        self,
        goal: str,
        recent_actions: str,
        workspace: str | None = None,
        inline_skill: str = "",
    ) -> dict:
        """B4 — periodic drift check on read-heavy streaks.

        Why this is its own method instead of reusing review_action:
        review_action's user message hardcodes ``Tool: <name>`` and
        ``Input: <json>`` because every per-action review is grading a
        SPECIFIC tool call. Passing tool_name='(self_check)' through that
        path produced JSON like ``{"decision": "approve"}`` ~always — the
        LLM had no anchor for *what* to grade. The B4 ship-dark bug.

        This method drops the tool framing entirely and asks the model
        to look at the trajectory: are the last N actions making progress
        toward the goal, or is Claude stuck looping / over-reading /
        researching without acting? Returns the same decision schema as
        review_action so the supervisor's elevation paths (escalate /
        correct / approve / request_evidence) work unchanged.
        """
        skills = _load_skills(workspace=workspace, inline_skill=inline_skill)
        user = (
            f"Goal: {goal}\n\n"
            f"Recent actions (last ~30):\n{recent_actions}\n\n"
            "Phase: drift check. Claude has just done several non-side-effecting "
            "actions in a row (Read / Grep / WebFetch / etc.) — no Write, Edit, "
            "or Bash. Look at the trajectory:\n"
            "  • Are these actions making progress toward the goal, or is Claude "
            "looping (re-reading the same file, re-grepping the same pattern)?\n"
            "  • Has Claude been researching for so long that it should now "
            "*act* — write code, run a verification, produce the deliverable?\n"
            "  • Is the chosen path the right one, or is it going down a rabbit "
            "hole the goal didn't ask for?\n\n"
            "Return JSON only: {\"decision\": \"approve\"|\"correct\"|\"escalate\"|"
            "\"request_evidence\", \"message\": \"...\"}.\n"
            "  • approve  → trajectory looks fine, keep going\n"
            "  • correct  → wrong direction; the message will be sent to Claude "
            "mid-stream as coaching\n"
            "  • escalate → blocked / needs user decision\n"
            "  • request_evidence → ambiguous; ask Claude to show its work"
        )
        raw = _chat(self.system, user, max_tokens=512, skills_context=skills)
        data = _extract_json(raw)
        return {
            "decision": data.get("decision", "approve"),
            "message": data.get("message", ""),
        }

    def final_review(
        self,
        goal: str,
        action_log: str,
        workspace: str | None = None,
        inline_skill: str = "",
        user_notes_history: list[str] | None = None,
    ) -> dict:
        """Full end-of-loop review. Returns
        {passed, summary, issues, next_steps}.

        Hard rules enforced before returning:
          1. passed=true requires BOTH (a) demonstrable verification
             evidence AND (b) zero issues.
          2. summary is REQUIRED — fall back to a 'reviewer did not
             produce a summary' issue if missing.
          3. next_steps is REQUIRED whenever passed=true.
          4. If only skills/SKILL.md exists and the goal asked for
             something else, mark passed=false.
          5. If a named deliverable file is missing from the workspace,
             mark passed=false.
        """
        skills = _load_skills(workspace=workspace, inline_skill=inline_skill)
        # Bug fix (live-task audit): the reviewer used to see only `goal`
        # — but mid-flight user notes (added via /note) often contain
        # the most recent specification ("change color to orange", "add
        # an EditText"). Without user_notes_history, the reviewer would
        # rubber-stamp passed=True as long as the original goal was met,
        # ignoring whether the notes were applied. Now we list every
        # note the user added and require the reviewer to verify each
        # was reflected in the deliverable.
        notes_block = ""
        if user_notes_history:
            notes_lines = "\n".join(f"  - {n}" for n in user_notes_history[:15])
            notes_block = (
                "\n\nMID-FLIGHT USER NOTES (must be reflected in the deliverable, "
                "in addition to the original goal):\n"
                f"{notes_lines}\n\n"
                "If ANY of these notes is not visibly reflected in the workspace "
                "artifacts (source code, layout, copy, etc.), set passed=false "
                "and include a clear issue naming which note was ignored.\n"
            )
        user = (
            f"Goal: {goal}\n"
            f"{notes_block}"
            f"\nFull action log + workspace artifacts:\n{action_log}\n\n"
            "Phase: final review. Output JSON only with this exact shape:\n"
            '{\n'
            '  "passed": true|false,\n'
            '  "summary": "<one paragraph: what was produced vs the goal>",\n'
            '  "issues": ["..."],\n'
            '  "next_steps": "<USER-FACING: how to actually USE the deliverable. Be specific.\n'
            '                 If it\'s a webapp: how to run it (exact command + port).\n'
            '                 If it\'s an Android app: how to build/install (./gradlew installDebug etc.).\n'
            '                 If it\'s a doc/research: where the file is + what it contains.\n'
            '                 If it depends on another service running: state that + the start command.\n'
            '                 Always include exact file paths and commands. Multi-line OK.>",\n'
            '  "deliverables": ["<workspace-relative-path>", ...]\n'
            '}\n\n'
            "Rules:\n"
            "1. passed=true ONLY if BOTH (a) demonstrable verification evidence exists, AND (b) zero issues.\n"
            "2. summary is REQUIRED — describe what was produced.\n"
            "3. next_steps is REQUIRED whenever passed=true — tell the user EXACTLY how to use the deliverable.\n"
            "   Imagine you handed off to a colleague who has never seen this. What 3-5 commands or links do they need?\n"
            "4. If only skills/SKILL.md exists and the goal asked for something else, mark passed=false.\n"
            "5. If a named deliverable file doesn't exist in the workspace, mark passed=false.\n"
            "\n"
            "DELIVERABLES (G10 — critical):\n"
            "The `deliverables` field is the EXACT list of workspace-relative file paths the user actually\n"
            "asked for. The Telegram bot will send these files (and ONLY these) back to the user as documents.\n"
            "  • Include: the apk, the pdf, the dataset, the script, the report — whatever the goal named.\n"
            "  • EXCLUDE: scaffolding (gradlew, build.gradle, package-lock.json, .gitignore, Dockerfile, etc.),\n"
            "    intermediate build outputs, duplicate copies of the same file at different paths, READMEs\n"
            "    unless the README itself was the deliverable, anything inside node_modules / dist / build / .git.\n"
            "  • If the goal didn't ask for any file (pure research / Q&A answered in chat), return an empty list [].\n"
            "  • If the user's goal was 'send me X', deliverables MUST contain X. If X doesn't exist in the\n"
            "    workspace, mark passed=false and explain in issues.\n"
            "  • Paths must be workspace-relative, no leading slash. Example: 'hello-world-debug.apk', not\n"
            "    '/Users/.../hello-world-debug.apk' and not 'workspace/hello-world-debug.apk'."
        )
        raw = _chat(self.system, user, max_tokens=2048, skills_context=skills)
        data = _extract_json(raw)
        issues = data.get("issues", []) or []
        passed = bool(data.get("passed", False))
        summary = (data.get("summary") or "").strip()
        next_steps = (data.get("next_steps") or "").strip()
        # G10: deliverables is workspace-relative paths the bot will hand
        # back. Normalize: drop empties, strip leading slashes, dedup,
        # cap at a sane upper bound so the LLM can't accidentally request
        # 100 files.
        raw_delivs = data.get("deliverables") or []
        if not isinstance(raw_delivs, list):
            raw_delivs = []
        deliverables: list[str] = []
        seen: set[str] = set()
        for p in raw_delivs:
            if not isinstance(p, str):
                continue
            p = p.strip().lstrip("/")
            if not p or p in seen:
                continue
            seen.add(p)
            deliverables.append(p)
            if len(deliverables) >= 10:
                break
        if not summary:
            summary = "(reviewer did not produce a summary)"
            issues = list(issues) + ["Reviewer summary missing — re-review required."]
            passed = False
        if issues:
            passed = False
        return {
            "passed": passed,
            "issues": issues,
            "summary": summary,
            "next_steps": next_steps,
            "deliverables": deliverables,
        }
