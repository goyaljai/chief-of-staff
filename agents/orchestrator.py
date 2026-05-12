"""Orchestrator — the manager LLM role.

Responsibilities, in the order they fire across a task lifecycle:

  1. think_and_ask        Phase 1 — meta-think + 3-5 clarifying questions.
                          One LLM call that does both. The skill_preview
                          it returns is stashed in the dependencies cache
                          so the matching /task/run reuses it without a
                          second meta-think round.
  2. generate_skill_brief Phase 2 — produce the per-task SKILL.md given
                          task + clarifications. Uses the skill_preview
                          from phase 1 if available; otherwise generates
                          from scratch.
  3. parse_dag            Optional — extract a `## Steps` block from the
                          executor brief into DAG step dicts for parallel
                          fan-out. Returns None when the task is
                          single-shot.
  4. build_brief          Phase 3 — produce the executor brief that gets
                          handed to Claude Code as its prompt. Includes
                          the optional `## Steps` block when 2+ steps are
                          genuinely independent.
  5. build_correction_prompt
                          Between correction loops — packages reviewer
                          issues, user notes, and an optional grounding
                          nudge into a continuation prompt.
  6. generate_grounding_nudge
                          Mid-loop 1:1 check-in — picks ONE intervention
                          based on what Claude has actually done.
  7. auto_resolve_escalation
                          When the user doesn't answer an escalation
                          within 30 minutes, the orchestrator picks A/B
                          itself based on what serves the goal better.
  8. find_promotable_lessons
                          Post-task — extract 0-3 generic lessons for the
                          skill_lessons table. Filtered aggressively;
                          most things are too task-specific to promote.
  9. answer_from_history  /ask synthesizer — answers questions using
                          retrieved past task snippets.
"""
from .llm import _chat, _extract_json
from .prompts import _load_prompt, _load_skills


class Orchestrator:
    """Manager role — meta-thinking, questions, brief, correction prompts,
    auto-resolve."""

    def __init__(self):
        self.system = _load_prompt("orchestrator")

    def think_and_ask(self, task: str, answers_so_far: dict | None = None,
                      max_questions: int = 5) -> dict:
        """G9 — adaptive questioning.

        Returns the SINGLE next-most-useful question, conditioned on the
        goal + whatever the user has already answered. Stops when the
        orchestrator decides nothing meaningful is left to ask.

        Returns dict with shape::

          {
            "skill_preview": str,   # populated only on first call (empty answers)
            "questions": list[str], # ≤1 element; [] when done
            "done": bool,           # True iff no more questions needed
            "asked_count": int,     # how many Qs already answered (echo)
          }

        Why iterative: the previous one-shot generator produced redundant
        questions like 'native or framework?' AND 'Android only? debug
        APK?' on the same prompt — the second was already implied by the
        first. With per-step generation conditioned on prior answers, the
        orchestrator can drop redundant follow-ups before asking them.

        max_questions caps total Qs across all rounds (defense vs the
        LLM looping). 5 was the prior implicit cap.
        """
        skills = _load_skills()
        answers = answers_so_far or {}
        asked_count = len(answers)
        is_first_call = asked_count == 0

        if asked_count >= max_questions:
            # Hard cap reached — never keep asking forever.
            return {"skill_preview": "", "questions": [], "done": True,
                    "asked_count": asked_count}

        # Render prior Q&A so the LLM can condition on it.
        if answers:
            prior_qa = "\n".join(
                f"  Q: {q}\n  A: {a}" for q, a in answers.items()
            )
            qa_block = f"\nAlready asked & answered ({asked_count}):\n{prior_qa}\n"
        else:
            qa_block = ""

        # T4: surface relevant cross-task memories from Mem0 cloud so
        # the orchestrator can skip questions whose answers are
        # already known from prior tasks (e.g. "user prefers minimal
        # blog post output" → don't ask about tone). No-op when
        # MEM0_API_KEY is unset. Only fire on the first call to keep
        # token + latency cost down.
        memory_block = ""
        if is_first_call:
            try:
                from services import memory as mem
                if mem.is_enabled():
                    memories = mem.get_relevant_memories(task, limit=5)
                    memory_block = mem.render_memory_block(memories)
                    if memory_block:
                        memory_block = "\n" + memory_block + "\n"
            except Exception:
                memory_block = ""

        meta_block = (
            "Step A: think about what this task actually requires. Identify:\n"
            "  - the kind of work (code build, research, writing, data, ops, etc.)\n"
            "  - the likely 'done' criteria\n"
            "  - failure patterns specific to THIS kind of task\n"
            "  - what verification will prove the goal was met\n"
            "  - gotchas and edge cases\n\n"
        ) if is_first_call else ""

        # Note: skill_preview is only meaningful on the first call (it
        # captures the meta-thinking once). Follow-up calls return
        # empty skill_preview to save tokens.
        user = (
            "Phase 1 — meta-think + clarify (adaptive, one question at a time).\n\n"
            f"User's task:\n{task}\n"
            f"{qa_block}"
            f"{memory_block}"
            "\n"
            f"{meta_block}"
            "Step B: decide whether ONE more clarifying question would meaningfully change "
            "how the work gets done.\n\n"
            "Rules for asking:\n"
            "  - Each question must be INDEPENDENT of prior answers — do not ask a question "
            "whose answer is already implied by the goal + prior answers.\n"
            "  - Skip the question if the answer is obvious from defaults, derivable from prior "
            "answers, or the user clearly doesn't care.\n"
            "  - If you've already asked 3 questions and none of them was 'what should done "
            "look like', ask that next.\n"
            f"  - Hard cap: at most {max_questions} questions total across all rounds. "
            f"Already asked: {asked_count}.\n\n"
            "Output JSON exactly:\n"
            "{\n"
            '  "skill_preview": "<concise markdown summary of your meta-thinking — only populate on the first call (when no prior answers)>",\n'
            '  "question": "<the SINGLE next question, or null if no more questions are needed>",\n'
            '  "done": true|false\n'
            "}\n"
            "Set done=true ONLY when no further question would meaningfully refine the brief. "
            "When done=true, question must be null."
        )
        raw = _chat(self.system, user, max_tokens=1024 if not is_first_call else 2048,
                    skills_context=skills)
        data = _extract_json(raw)

        question = (data.get("question") or "").strip()
        done = bool(data.get("done", False))
        skill_preview = (data.get("skill_preview") or "").strip() if is_first_call else ""

        # Self-consistency: if done is true, drop any question; if a
        # question came back without explicit done, treat it as not-done.
        if done:
            question = ""
        questions = [question] if question else []
        if not question and not done:
            # LLM gave neither — treat as done to avoid a loop.
            done = True

        return {
            "skill_preview": skill_preview,
            "questions": questions,
            "done": done,
            "asked_count": asked_count,
        }

    def generate_skill_brief(
        self,
        task: str,
        clarifications: dict[str, str],
        skill_preview: str = "",
        library_match: dict | None = None,
    ) -> str:
        """Phase 2 — produce the final SKILL.md given task + answers.
        Follows Anthropic's skill-creator format: YAML frontmatter + body.
        If skill_preview (from think_and_ask) is provided, refine it
        instead of regenerating — saves an LLM round of meta-thinking."""
        skills = _load_skills()
        clarif_text = "\n".join(f"- Q: {q}\n  A: {a}" for q, a in clarifications.items()) or "(none)"
        library_section = ""
        if library_match:
            library_section = (
                "\n\nA past task had a similar SKILL.md you can reuse as a starting point "
                f"(matched on description, distance={library_match.get('distance', 0):.3f}):\n\n"
                f"{(library_match.get('meta') or {}).get('skill_md_preview', '')[:1500]}\n\n"
                "Refine that for THIS task. If the past skill doesn't actually fit, ignore it.\n"
            )
        if skill_preview:
            user = (
                "Phase 2 — refine your earlier meta-thinking into the final SKILL.md.\n\n"
                f"Task: {task}\n\n"
                f"Your earlier preview (from question-asking phase):\n{skill_preview}\n"
                f"{library_section}\n"
                f"Clarifications you got from the user:\n{clarif_text}\n\n"
                "REFINE the preview into a final SKILL.md, incorporating what the clarifications tell you. "
                "Don't restart from zero — reuse the structure and insights from the preview, sharpen them with the answers.\n\n"
                "Output the SKILL.md exactly in this format:\n\n"
                "```\n"
                "---\n"
                "name: <short-kebab-case-id-derived-from-task>\n"
                "description: <one sentence saying when to apply this skill — be 'pushy' (Anthropic skill-creator convention) so it triggers reliably on similar future tasks>\n"
                "---\n\n"
                "# <Title>\n\n"
                "## Objective\n<concrete goal for THIS task>\n\n"
                "## What 'done' means\n<verifiable end state>\n\n"
                "## Knowledge / standards that apply\n<only what's relevant>\n\n"
                "## Failure patterns to watch for\n<specific to this kind of work>\n\n"
                "## Verification required\n<what proof you'll demand>\n\n"
                "## Gotchas\n<common mistakes>\n\n"
                "## Scope boundaries\n<in / out>\n"
                "```\n\n"
                "Output only the SKILL.md content (frontmatter + body). No preamble, no code fences around the whole thing."
            )
        else:
            user = (
                "Phase 2 — generate the SKILL.md for this task (Anthropic skill-creator format).\n\n"
                f"Task: {task}\n\n"
                f"{library_section}\n"
                f"Clarifications:\n{clarif_text}\n\n"
                "Output the SKILL.md exactly in this format:\n\n"
                "```\n"
                "---\n"
                "name: <short-kebab-case-id-derived-from-task>\n"
                "description: <one sentence saying when to apply this skill — be 'pushy' so it triggers reliably on similar future tasks>\n"
                "---\n\n"
                "# <Title>\n\n"
                "## Objective\n## What 'done' means\n## Knowledge / standards that apply\n"
                "## Failure patterns to watch for\n## Verification required\n## Gotchas\n## Scope boundaries\n"
                "```\n\n"
                "Be specific to THIS task. Output only the SKILL.md content. No preamble."
            )
        return _chat(self.system, user, max_tokens=3500, skills_context=skills).strip()

    def parse_dag(self, brief: str) -> list[dict] | None:
        """Parse the optional `## Steps` section into DAG step dicts.

        Accepted line forms (annotation block at end is optional;
        key:value pairs separated by `;`):
            - id: action
            - id: action (deps: a, b)
            - id: action (timeout: 1800)
            - id: action (deps: a, b; timeout: 1800)
        """
        import re
        m = re.search(
            r"##\s+(?:Steps|DAG|Plan)\s*\n(.*?)(?:\n##\s|\Z)",
            brief,
            re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return None
        body = m.group(1)
        steps: list[dict] = []
        line_re = re.compile(r"^\s*[-*]?\s*([A-Za-z0-9_\-]+):\s*(.+?)\s*(?:\(([^)]*)\))?\s*$")
        for line in body.splitlines():
            mm = line_re.match(line)
            if not mm:
                continue
            sid, action, annot = mm.group(1), mm.group(2).strip(), mm.group(3)
            step: dict = {"id": sid, "action": action, "depends_on": []}
            if annot:
                for piece in annot.split(";"):
                    piece = piece.strip()
                    if not piece or ":" not in piece:
                        continue
                    key, _, val = piece.partition(":")
                    key, val = key.strip().lower(), val.strip()
                    if key in ("dep", "deps"):
                        step["depends_on"] = [d.strip() for d in val.split(",") if d.strip()]
                    elif key == "timeout":
                        try:
                            step["timeout_secs"] = max(60, int(val))
                        except ValueError:
                            pass
            steps.append(step)
        return steps or None

    def build_brief(
        self,
        task: str,
        clarifications: dict[str, str],
        workspace: str,
        inline_skill: str = "",
        env_audit: str = "",
    ) -> str:
        """Build the executor brief — the prompt that gets handed to Claude
        Code. The brief MUST name explicit deliverable files because the
        executor's job is to produce those files in the workspace.

        Parameters
        ----------
        env_audit : str
            Optional pre-rendered markdown block from
            ``services.env_audit.render_brief_block`` describing which
            toolchains are available on the executor's machine (G8). Pass
            an empty string to skip — the brief will then be built without
            any environment context. When provided, the orchestrator gets
            it inline so it can pivot the deliverable shape (e.g. propose
            Expo Go QR when ANDROID_HOME is missing) instead of letting
            Claude grind on a doomed scaffold.
        """
        skills = _load_skills(workspace=workspace, inline_skill=inline_skill)
        clarif_text = "\n".join(f"- Q: {q}\n  A: {a}" for q, a in clarifications.items()) or "(none)"
        # G8: Inject the env-audit markdown into the user prompt right
        # after the clarifications. Empty string → no extra block, brief
        # behaves identically to pre-G8.
        env_block = f"\n{env_audit}\n" if env_audit else ""
        user = (
            "Phase 3 — build the executor brief.\n\n"
            f"Task: {task}\n\n"
            f"Clarifications:\n{clarif_text}\n"
            f"{env_block}\n"
            "Write a precise brief that an executor (Claude Code) can run with no further questions. "
            "Use the Skill brief above as your authoritative guide. Structure:\n"
            "- Objective\n"
            "- **Deliverable file(s)** — exact filename(s) the executor must produce in the workspace. Be explicit.\n"
            "- What needs to be built / produced\n"
            "- Done / acceptance criteria (numbered, verifiable)\n"
            "- Constraints\n"
            "- Quality bar\n"
            "- Recommended implementation shape (only if useful)\n\n"
            "Be explicit about deliverable files. The executor's job is to produce those files. "
            "The workspace will be empty when the executor starts — anything it should produce, name it explicitly.\n\n"
            "PARALLEL DAG (optional, only when warranted):\n"
            "If the task decomposes into 2+ steps that can run independently AND each step "
            "produces a distinct artifact (e.g. compile vs lint, fetch vs transform vs load, "
            "build N independent files), append a final section formatted EXACTLY:\n\n"
            "## Steps\n"
            "- step_id_1: short imperative action sentence\n"
            "- step_id_2: short imperative action sentence (deps: step_id_1)\n"
            "- step_id_3: heavier action sentence (deps: step_id_1; timeout: 1800)\n\n"
            "Rules:\n"
            "- Step IDs MUST match `[A-Za-z0-9_-]{1,64}` (no spaces, no slashes, no dots).\n"
            "- Annotation block `(...)` is optional; pairs separated by `;`.\n"
            "  - `deps: a, b` → dependencies (defaults to none).\n"
            "  - `timeout: N` → seconds, only set when a step is genuinely long-running (build, install, large compile). Default is 600s.\n"
            "- Each step's action becomes a SEPARATE Claude Code subprocess in the SAME workspace directory.\n"
            "- Do NOT emit `## Steps` for tasks that are inherently single-shot (one file, one essay, one analysis). "
            "Emit it only when 2+ steps are genuinely independent or when there's a clear topological order with parallel branches.\n\n"
            "Output markdown only, no preamble."
        )
        return _chat(self.system, user, max_tokens=4096, skills_context=skills).strip()

    def build_correction_prompt(
        self,
        brief: str,
        supervisor_issues: list[str],
        user_notes: list[str] | None = None,
        grounding_nudge: str = "",
    ) -> str:
        """Build the prompt for a correction loop — packages reviewer
        issues, user notes (mid-task), and an optional grounding nudge."""
        issues_text = "\n".join(f"- {i}" for i in supervisor_issues) or "- (no specific issues recorded)"
        notes_section = ""
        if user_notes:
            notes_text = "\n".join(f"- {n}" for n in user_notes)
            notes_section = (
                "\n\nThe user also added these notes mid-task — fold them into your continued work:\n"
                f"{notes_text}\n"
            )
        nudge_section = ""
        if grounding_nudge:
            nudge_section = (
                "\n\n--- Manager check-in (1:1 from your orchestrator) ---\n"
                f"{grounding_nudge}\n"
                "Pause and answer this in your head before you continue.\n"
                "----------------------------------------\n"
            )
        return (
            "Continue working on the original task. The independent reviewer found issues "
            "in your previous attempt that must be fixed before the work can be marked done.\n\n"
            f"Original brief:\n{brief}\n\n"
            f"Issues to fix:\n{issues_text}{notes_section}{nudge_section}\n\n"
            "Address every issue. If user notes were provided, fold them in too. "
            "After fixing, run the verification step and show its output."
        )

    def generate_grounding_nudge(
        self,
        task: str,
        brief: str,
        action_log_summary: str,
        loop_num: int,
    ) -> str:
        """Periodic 1:1 between orchestrator and Claude — manager-style
        check-in. Picks ONE grounding question or directive based on what
        Claude has done so far. Returns a 1-3 sentence nudge."""
        skills = _load_skills()
        user = (
            "You're acting as Claude Code's manager doing a brief 1:1 check-in. "
            "You've watched Claude work for a loop. Pick ONE grounding intervention that would actually help — "
            "either a question that exposes a hidden assumption, a directive to validate something specific, "
            "or a nudge to refocus on the original goal. Keep it short (1-3 sentences). Be specific to what "
            "Claude has actually done, not generic.\n\n"
            f"Original task: {task}\n\n"
            f"Brief excerpt:\n{brief[:1500]}\n\n"
            f"What Claude did in loop {loop_num} (recent actions):\n{action_log_summary[:2000]}\n\n"
            "Output JUST the nudge text — no preamble, no JSON, just the words you'd say to Claude."
        )
        return _chat(self.system, user, max_tokens=300, skills_context=skills).strip()

    def auto_resolve_escalation(
        self,
        goal: str,
        brief: str,
        action_log_summary: str,
        question: str,
        option_a: str,
        option_b: str,
        workspace: str | None = None,
    ) -> str:
        """When the user doesn't answer in 30 min, pick A or B based on
        which serves the goal better."""
        skills = _load_skills(workspace=workspace)
        user = (
            "An escalation timed out (user did not respond within 30 minutes). "
            "Make the best judgment for the user given the full context. "
            "Pick option A or option B based on which serves the goal better.\n\n"
            f"Goal: {goal}\n\n"
            f"Brief: {brief}\n\n"
            f"What has happened so far:\n{action_log_summary}\n\n"
            f"Escalation question: {question}\n"
            f"A) {option_a}\n"
            f"B) {option_b}\n\n"
            'Respond with JSON only: {"choice": "a" or "b", "rationale": "one sentence"}'
        )
        raw = _chat(self.system, user, max_tokens=512, skills_context=skills)
        data = _extract_json(raw)
        choice = (data.get("choice") or "a").lower()
        return choice if choice in ("a", "b") else "a"

    def find_promotable_lessons(
        self,
        task: str,
        skill_md: str,
        brief: str,
        action_log_summary: str,
        review_summary: str,
        review_issues: list[str],
        passed: bool,
        workspace: str | None = None,
    ) -> list:
        """Post-task — identify lessons GENERAL enough to apply to future
        DIFFERENT tasks. Filter aggressively. Return list of 0-3 entries
        (each a dict with pattern + optional remediation + domains)."""
        skills = _load_skills(workspace=workspace)
        outcome = "PASSED" if passed else "FAILED"
        issues_text = "\n".join(f"- {i}" for i in review_issues[:5]) or "- (none)"
        user = (
            "Phase 4 — promote lessons.\n\n"
            "A task just completed. Identify lessons GENERAL enough to apply to future DIFFERENT tasks. "
            "Filter aggressively — most things are too specific. We promote only if a future "
            "task in a different domain would benefit. Avoid restating things already in the global skills above.\n\n"
            f"Task: {task}\n"
            f"Outcome: {outcome}\n"
            f"Skill brief that guided this task:\n{skill_md[:2000]}\n\n"
            f"Brief excerpt:\n{brief[:1000]}\n\n"
            f"Action log summary:\n{action_log_summary[:2000]}\n\n"
            f"Review summary: {review_summary}\n"
            f"Issues caught:\n{issues_text}\n\n"
            "Output JSON: {\"lessons\": [{\"pattern\": \"...\", \"remediation\": \"...\", \"domains\": [\"code\"|\"research\"|\"data\"|\"ops\"|\"writing\"]}, ...]}.\n"
            "Each lesson:\n"
            "  - `pattern` (required): one sentence, actionable rule for a future reviewer/orchestrator. General — applies to a DIFFERENT task, not just a similar one. NOT already in the global skills above.\n"
            "  - `remediation` (optional but encouraged): one sentence telling the executor HOW to satisfy the rule when it kicks in.\n"
            "  - `domains` (optional): which task types this applies to, from {code, research, data, ops, writing}. Empty list = universal.\n"
            "Empty `lessons` list is the right answer most of the time."
        )
        raw = _chat(self.system, user, max_tokens=1024, skills_context=skills)
        data = _extract_json(raw)
        lessons = data.get("lessons") or []
        out: list = []
        for l in lessons:
            if isinstance(l, str) and l.strip():
                out.append(l.strip())  # legacy bare-string fallback
            elif isinstance(l, dict) and (l.get("pattern") or "").strip():
                out.append({
                    "pattern": str(l["pattern"]).strip(),
                    "remediation": (l.get("remediation") or "").strip() or None,
                    "domains": [d for d in (l.get("domains") or []) if isinstance(d, str)],
                })
        return out[:3]

    def answer_from_history(self, question: str, retrieved: list[dict]) -> dict:
        """Synthesize a /ask answer from retrieved past task snippets.
        Returns {answer, cited_task_ids}."""
        if not retrieved:
            return {
                "answer": "I have no past tasks matching that question yet. Try running a relevant task first.",
                "cited_task_ids": [],
            }
        ctx = []
        for i, r in enumerate(retrieved[:6]):
            tid = r.get("task_id") or (r.get("meta") or {}).get("task_id") or "?"
            goal = r.get("goal") or (r.get("meta") or {}).get("goal") or ""
            snip = r.get("doc") or r.get("snip") or r.get("brief") or ""
            ctx.append(f"[Source {i+1}] task_id={tid}\nGoal: {goal}\nContent: {snip[:1500]}")
        sources_block = "\n\n".join(ctx)
        user = (
            f"User question: {question}\n\n"
            f"Past tasks retrieved from history (most relevant first):\n\n{sources_block}\n\n"
            "Answer the user's question using ONLY these sources. Be concise. "
            'If the sources cover it, give a direct answer and end with "Sources: <task_id>, <task_id>". '
            'If the sources do not cover it, say so directly — do not fabricate.\n\n'
            'Output JSON: {"answer": "...", "cited_task_ids": ["...", "..."]}'
        )
        raw = _chat(self.system, user, max_tokens=1024)
        data = _extract_json(raw)
        return {
            "answer": (data.get("answer") or raw or "").strip(),
            "cited_task_ids": data.get("cited_task_ids") or [],
        }
