"""The supervision loop. Glues together:
  Orchestrator (manager LLM) → builds brief, builds corrections
  ClaudeRunner               → runs Claude headlessly, streams events
  Reviewer (independent QA)  → per-action and final reviews (blind to brief)
  TaskStore                  → state, escalation, log
The HUMAN USER is the Supervisor. This loop runs on their behalf.
"""
import asyncio
import json
import os
import re
import time
from pathlib import Path

from runners import ClaudeEvent, ClaudeRunner
from config import (
    ESCALATION_AUTO_RESOLVE_SECS,
    LOG_ROOT,
    MAX_CORRECTION_LOOPS,
)
from agents import Orchestrator, Reviewer, append_to_global, save_task_skill, set_usage_callback
from persistence import STORE, TaskState
import workflows.dag as dag_executor
import rag
from services.tool_review import classify_announced_tools, is_side_effecting


# B1 (mid-stream interrupt + coach): cap on per-loop mid-stream interrupts.
# Each one costs an interrupt + new Claude subprocess + --resume round-trip.
# 3 is enough to recover from a couple of false starts; beyond that the
# coaching is probably wrong about what's wrong, and we should fall through
# to the correction-loop boundary instead of ping-ponging.
MAX_MID_STREAM_INTERRUPTS = 3

# B4 (conditional reviewer self-check): on read-heavy tasks (lots of Read /
# Grep / WebFetch with no Write/Edit/Bash), the per-action gate from B1
# never fires — Claude can drift for many tool calls without any reviewer
# pass. After this many CONSECUTIVE non-side-effecting tool_uses, we run a
# lightweight reviewer pass over the recent log. The streak resets every
# time a side-effecting tool fires (since that already triggers per-action
# review).
SELF_CHECK_AFTER_N_NON_REVIEWED = 4

# Bound the cost of self-checks per outer correction loop. Each pass is
# one Reviewer LLM call (~512 output tokens). 4 passes/loop ≈ 2K tokens —
# cheap insurance against silent drift, but capped so a 200-tool-call task
# doesn't run 50 reviewer calls.
MAX_SELF_CHECKS_PER_LOOP = 4


def _summarize_log(log: list[dict], limit: int = 80, full_text: bool = False) -> str:
    """Build a summary of recent log entries. When full_text=True, do NOT truncate
    text/result/tool_result content — used at final_review time so the reviewer sees
    the actual artifacts, not a truncated approximation."""
    lines = []
    text_cap = 6000 if full_text else 200
    result_cap = 6000 if full_text else 200
    for entry in log[-limit:]:
        kind = entry.get("kind")
        if kind == "tool_use":
            tname = entry.get("tool")
            tinput = json.dumps(entry.get("input") or {})[:300]
            lines.append(f"[tool] {tname}: {tinput}")
        elif kind == "tool_result":
            out = (entry.get("output") or "")[:result_cap]
            err = " (ERROR)" if entry.get("is_error") else ""
            lines.append(f"[result]{err} {out}")
        elif kind == "text":
            lines.append(f"[claude] {(entry.get('text') or '')[:text_cap]}")
        elif kind == "result":
            lines.append(f"[final-text] {(entry.get('text') or '')[:text_cap]}")
        elif kind == "reviewer":
            lines.append(f"[reviewer:{entry.get('decision')}] {(entry.get('message') or '')[:300]}")
        elif kind == "hook":
            lines.append(f"[hook:{entry.get('decision')}] {entry.get('tool')} -> {(entry.get('reason') or '')[:120]}")
    return "\n".join(lines) or "(no actions)"


# Binary artifacts whose final-output presence is itself the deliverable
# for many task families — APK / IPA / JAR / WAR / archive / PDF / etc.
# These get listed as `path (Nbytes, binary)` regardless of whether their
# directory is on the noise skip-list. Without this, an Android task's
# real APK at `app/build/outputs/apk/debug/app-debug.apk` would be hidden
# from the reviewer (which lives in skip_dir_names → "build") and the
# reviewer would correctly but unhelpfully reject the task with "APK
# missing" while the binary was sitting on disk the whole time.
_BINARY_DELIVERABLE_SUFFIXES = {
    ".apk", ".aab", ".ipa", ".jar", ".war", ".dmg", ".pkg",
    ".zip", ".tar", ".tgz", ".tar.gz",
    ".pdf", ".epub",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".mp4", ".mov", ".m4a", ".mp3", ".wav",
    ".so", ".dylib", ".dll", ".exe",
    ".whl", ".gem",
}


def _is_binary_deliverable(name: str) -> bool:
    n = name.lower()
    if n.endswith(".tar.gz"):
        return True
    return any(n.endswith(s) for s in _BINARY_DELIVERABLE_SUFFIXES)


def _parse_brief_deliverables(brief: str) -> list[str]:
    """Extract the file paths declared under the brief's
    `## Deliverable file(s)` section.

    Why this exists (audit r3, deeper-fix Approach A): rather than have
    the supervisor scan the workspace and let the reviewer guess what
    the deliverable is, we trust the orchestrator's brief — which
    already names exact deliverable paths — as ground truth. Pass those
    paths directly to the reviewer (after verifying existence) so the
    reviewer doesn't have to infer from a sea of artifact lines whether
    `app-debug.apk` was the user's actual ask.

    Returns a deduped list of paths. Empty when no Deliverables section
    is present (research / inline-answer tasks).
    """
    if not brief:
        return []
    lines = brief.splitlines()
    # Find the section header (any heading level, case-insensitive,
    # singular or plural, with or without "(s)").
    in_section = False
    paths: list[str] = []
    seen: set[str] = set()
    # Bug fix (audit 5-r): accept all common spellings of the heading —
    # "Deliverable", "Deliverables", "Deliverable files", "Deliverable
    # file(s)", "Deliverable Files", optional trailing colon. The
    # orchestrator brief template currently uses "Deliverable file(s)"
    # but a small wording change shouldn't silently disable parsing.
    heading_re = re.compile(
        r"^#{1,6}\s*Deliverable(?:s|\s+files?(?:\(s\))?)?\s*:?\s*$",
        re.IGNORECASE,
    )
    for line in lines:
        stripped = line.strip()
        if heading_re.match(stripped):
            in_section = True
            continue
        if not in_section:
            continue
        # Section ends at the next heading.
        if re.match(r"^#{1,6}\s+\S", stripped):
            break
        m = re.match(r"^[\-\*\+]\s+(.+)$", stripped)
        if not m:
            continue
        candidate = m.group(1).strip()
        candidate = candidate.strip("`").strip()
        # Drop trailing "— description" or " - explanation" annotations.
        candidate = re.split(r"\s+[—–\-]\s+", candidate, maxsplit=1)[0].strip()
        candidate = candidate.strip("`").strip()
        if not candidate or candidate.startswith("("):
            continue
        # Reject obvious non-paths (parens, sentences, urls).
        if " " in candidate and "/" not in candidate and "." not in candidate:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        paths.append(candidate)
        if len(paths) >= 10:
            break
    return paths


def _parse_executor_deliverable_marker(task_log: list[dict]) -> list[str]:
    """Extract Claude's own end-of-run `DELIVERABLE_PATHS: a, b, c`
    declaration from the task log.

    Approach B (audit r3, deeper-fix): rather than have the supervisor
    guess what Claude built, the brief instructs Claude to emit a
    machine-readable marker line at the end of its run naming the
    user-facing deliverable file(s). When Claude is well-behaved we
    trust this list as authoritative — it captures what Claude
    actually produced (which may differ from what the brief originally
    asked for if the deliverable shape pivoted mid-run, e.g. rust
    binary → rust source files when the toolchain was missing).

    Returns a deduped list of paths, or [] when no marker was found.
    Scans the most recent text events first so a re-tried run wins
    over an aborted earlier attempt's marker.
    """
    if not task_log:
        return []
    pat = re.compile(
        r"DELIVERABLE_PATHS\s*[:=]\s*([^\n\r]+)",
        re.IGNORECASE,
    )
    for entry in reversed(task_log):
        if not isinstance(entry, dict):
            continue
        text = entry.get("text") or entry.get("content") or ""
        if not text or "DELIVERABLE_PATHS" not in text.upper():
            continue
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1).strip()
        # Audit 5-r bug fix: prefer backtick-delimited extraction when
        # Claude wraps each path in `backticks` (the brief template
        # encourages this) — that tolerates paths with embedded
        # spaces. Fall back to comma split for the unwrapped case.
        # Note: filenames with embedded commas are rare enough that we
        # don't try to handle them in the comma path; clean filenames
        # are the norm and the brief template recommends backticks.
        backtick_parts = re.findall(r"`([^`]+)`", raw)
        if backtick_parts:
            parts = backtick_parts
        else:
            raw_clean = raw.strip("[]").strip()
            parts = [p.strip() for p in raw_clean.split(",")]
        parts = [p.strip().strip("`").strip("'\"") for p in parts]
        out: list[str] = []
        seen: set[str] = set()
        for p in parts:
            if not p or p.lower() in ("none", "n/a", "(none)", "[]"):
                continue
            if p in seen:
                continue
            seen.add(p)
            out.append(p)
            if len(out) >= 10:
                break
        return out
    return []


def _render_executor_declared(workspace: Path, paths: list[str]) -> str:
    """Format Claude's own end-of-run DELIVERABLE_PATHS list + on-disk
    existence as a markdown block. Reviewer trusts this over its own
    inference when present."""
    if not paths:
        return ""
    lines = ["## Executor-declared deliverables (DELIVERABLE_PATHS marker — authoritative)"]
    for p in paths:
        rel = p.lstrip("/")
        abs_path = workspace / rel
        try:
            if abs_path.is_file():
                size = abs_path.stat().st_size
                lines.append(f"- `{rel}` → EXISTS ({size:,} bytes)")
            elif abs_path.exists():
                lines.append(f"- `{rel}` → exists but is not a file")
            else:
                lines.append(f"- `{rel}` → MISSING from workspace")
        except Exception as e:
            lines.append(f"- `{rel}` → check failed: {e}")
    lines.append(
        "\nThe executor declared these as the user-facing deliverables. "
        "Trust this list as authoritative — set `passed=true` and copy "
        "these paths into `deliverables` when each EXISTS. If any are "
        "MISSING, set `passed=false` and explain which path is wrong."
    )
    return "\n".join(lines)


def _render_declared_deliverables(workspace: Path, paths: list[str]) -> str:
    """Format the brief-declared deliverables + their on-disk existence
    as a markdown block to append to the reviewer's input. Empty when
    no paths were declared."""
    if not paths:
        return ""
    lines = ["## Declared deliverables (per brief — ground truth)"]
    for p in paths:
        rel = p.lstrip("/")
        abs_path = workspace / rel
        try:
            if abs_path.is_file():
                size = abs_path.stat().st_size
                lines.append(f"- `{rel}` → EXISTS ({size:,} bytes)")
            elif abs_path.exists():
                lines.append(f"- `{rel}` → exists but is not a file (directory or special)")
            else:
                lines.append(f"- `{rel}` → MISSING from workspace")
        except Exception as e:
            lines.append(f"- `{rel}` → check failed: {e}")
    lines.append(
        "\nThese are the files the brief instructed the executor to "
        "produce. If they exist, accept them as the deliverable and "
        "set `passed=true` + `deliverables=[...]` accordingly. If they "
        "are MISSING, that is a hard fail."
    )
    return "\n".join(lines)


def _list_workspace_artifacts(workspace: Path, max_files: int = 30, max_bytes_per_file: int = 60000) -> str:
    """List interesting artifact files in the workspace and inline their content for the reviewer.

    V3 bug fix #1: cap raised from 8KB to 60KB so medium-sized markdown/code files aren't truncated.
    V3 bug fix #2: max_files raised 8 → 30. Android projects have 15-25 files; truncating at 8
    caused reviewer to say 'MainActivity.kt missing' when only gradle config files showed up.
    Skips skills/, .claude/, hidden dirs, and known build noise.

    Bug fix (Phase 3 audit r3, build-output): the skip list contains
    ``build`` to keep gradle/webpack intermediates out of the reviewer's
    window, but on Android tasks the actual APK lives at
    ``app/build/outputs/apk/debug/app-debug.apk`` — under ``build`` —
    and was being filtered out. The reviewer then correctly but
    unhelpfully said 'APK missing' on a workspace where the APK was
    present. We now allow-list binary deliverable suffixes inside
    otherwise-skipped dirs and list them as ``path (Nbytes, binary)``
    rather than trying to read their content (which would garble for
    a multi-megabyte binary).
    """
    if not workspace.exists():
        return "(workspace missing)"
    skip_dir_names = {"skills", ".claude", ".gradle", ".idea", "build", "node_modules", "__pycache__", "venv", ".venv"}
    files: list[Path] = []
    binaries: list[Path] = []
    for p in workspace.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(workspace)
        if rel.name.startswith("."):
            continue
        in_skip_dir = any(
            part in skip_dir_names or part.startswith(".")
            for part in rel.parts[:-1]
        )
        if _is_binary_deliverable(rel.name):
            # Always surface binary deliverables, even when their parent
            # dir is on the skip list (build/, dist/, target/ etc.).
            binaries.append(p)
            continue
        if in_skip_dir:
            continue
        files.append(p)
    files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    binaries = sorted(binaries, key=lambda p: p.stat().st_size, reverse=True)[:10]
    if not files and not binaries:
        return "(no user-facing artifacts found)"
    lines = []
    for p in binaries:
        size = p.stat().st_size
        # No content read — these are real binary outputs and reading
        # them would either OOM the prompt or feed Claude garbled bytes.
        lines.append(f"### {p.relative_to(workspace)} ({size}B, binary deliverable)")
    for p in files:
        try:
            size = p.stat().st_size
            content = p.read_text(errors="replace") if size < 200_000 else "(file too large)"
            content = content[:max_bytes_per_file]
            lines.append(f"### {p.relative_to(workspace)} ({size}B)\n```\n{content}\n```")
        except Exception as e:
            lines.append(f"### {p.relative_to(workspace)} (read error: {e})")
    return "\n\n".join(lines)


def _classify_install_risk(option_a: str) -> str:
    """Classify the install-command in option_a as 'low' or 'high' risk.

    Low-risk = a single, well-known package-manager invocation that
    completes in seconds-to-a-minute, doesn't require sudo, doesn't
    require license acceptance, and is easily reversible. The
    supervisor's auto-resolve path (gated by
    ``COS_AUTO_RESOLVE_ESCALATIONS=1``) will pick option_a without
    paging the user when this returns 'low'.

    High-risk = anything sudo / multi-GB / license-prompted / long /
    sweeping (Android SDK, Xcode CLT install, anything writing to
    /etc, anything piping curl into shell). The user must approve.

    Conservative — defaults to 'high' on anything ambiguous so the
    user is never surprised by an autonomous install they didn't
    expect.
    """
    if not option_a:
        return "high"
    s = option_a.lower()
    # Hard-deny: anything sudo / system-modifying / license-prompted.
    high_risk_markers = (
        "sudo ",
        "apt-get ", "apt install", "yum ", "dnf ",
        "pacman -", "zypper ",
        "softwareupdate ",
        "xcode-select --install",
        "android-commandlinetools", "android-sdk", "androidsdk",
        "/etc/", "license", "accept",
        "curl ", "wget ",  # raw curl|sh isn't whitelisted even though brew uses it internally
        "rm -rf",
    )
    if any(m in s for m in high_risk_markers):
        return "high"
    # Allow-list: known low-risk single-package installers.
    low_risk_markers = (
        "brew install ",
        "brew tap",
        "npm install -g ", "npm i -g ",
        "pnpm add -g ", "yarn global add",
        "pip install ", "pip3 install ", "python3 -m pip install ",
        "pipx install ",
        "cargo install ",
        "go install ",
        "rustup ",
        "nvm install ",
        "asdf install ",
    )
    if any(m in s for m in low_risk_markers):
        return "low"
    return "high"


def _parse_escalation(message: str) -> dict:
    """Parse a free-text or structured escalation into a typed dict.

    Two formats are recognised:

    1. **Generic escalation** — any message with `A) <text>` and
       `B) <text>` lines. Returns
       ``{kind: 'general', question, option_a, option_b}``.

    2. **Environment escalation (G7+)** — message starts with the
       literal marker ``ESCALATION:`` and has an ``OPTIONS:`` block
       with ``A) ...``, ``B) ...``, and an ``ABORT) ...`` line. The
       brief tells Claude to use this exact format whenever an env
       wall blocks the deliverable. Returns
       ``{kind: 'environment', question, summary, why, option_a,
          option_b, option_abort}`` so the UI can render three buttons
       (install / fallback / abort) instead of generic A/B.

    The two shapes share ``question, option_a, option_b`` so existing
    UI/bot escalation handlers keep working — they'll just ignore the
    extra fields. Tier-up clients render the env-specific fields.
    """
    text = message.strip()
    lines = text.splitlines()

    # Generic A) / B) extractor — used for both shapes.
    # Bug fix (Phase 3 audit r2): the previous regex `^[\s\*\-]*A[\)\.]`
    # matched any line beginning with "A." or "A)" — including prose
    # like "A.I. is interesting." (line starts with "A.", remaining
    # text "I. is interesting" became option_a). Require a whitespace
    # or end-of-string after the delimiter so structured "A) install"
    # matches but "A.I." does not (no space after the period in the
    # acronym).
    option_a = next((re.sub(r"^[\s\*\-]*A[\)\.]\s+", "", l).strip()
                     for l in lines if re.match(r"^[\s\*\-]*A[\)\.](?:\s+|$)", l)),
                    "Proceed as planned")
    option_b = next((re.sub(r"^[\s\*\-]*B[\)\.]\s+", "", l).strip()
                     for l in lines if re.match(r"^[\s\*\-]*B[\)\.](?:\s+|$)", l)),
                    "Stop and wait for clarification")

    # G7+ env-escalation marker. Trigger on either an explicit
    # 'ESCALATION:' header line OR an ABORT) option (only env path
    # produces ABORT).
    escalation_header = next(
        (re.sub(r"^[\s\*\-]*ESCALATION[:\s]*", "", l).strip()
         for l in lines if re.match(r"^[\s\*\-]*ESCALATION[:\s]", l, re.IGNORECASE)),
        "",
    )
    why_block = ""
    in_why = False
    why_buf: list[str] = []
    for l in lines:
        s = l.strip()
        if re.match(r"^[\s\*\-]*WHY[:\s]", s, re.IGNORECASE):
            in_why = True
            stripped = re.sub(r"^[\s\*\-]*WHY[:\s]*", "", s).strip()
            if stripped:
                why_buf.append(stripped)
            continue
        if in_why:
            # Same Bug 5 tightening — require a whitespace after the
            # A./B. delimiter so prose acronyms don't end the WHY block
            # prematurely.
            if re.match(r"^[\s\*\-]*OPTIONS[:\s]", s, re.IGNORECASE) or re.match(r"^[\s\*\-]*[ABab][\)\.](?:\s+|$)", s):
                in_why = False
                continue
            if s:
                why_buf.append(s)
    why_block = "\n".join(why_buf).strip()
    option_abort = next(
        (re.sub(r"^[\s\*\-]*ABORT[\)\.]\s*", "", l).strip()
         for l in lines if re.match(r"^[\s\*\-]*ABORT[\)\.]", l, re.IGNORECASE)),
        "",
    )

    # Bug fix (Phase 3 audit): kind=environment used to fire on JUST
    # `escalation_header OR option_abort`, which means any prose line
    # starting with "ESCALATION:" — even Claude saying "I considered
    # ESCALATION: but decided not to" — would trigger a fake env wall.
    # The structured format the orchestrator brief actually instructs
    # Claude to emit always has all of ESCALATION: + WHY: + OPTIONS:
    # together. Require all three before classifying as environment.
    has_options = any(
        re.match(r"^[\s\*\-]*OPTIONS[:\s]", line, re.IGNORECASE)
        for line in lines
    )
    is_structured_env = bool(escalation_header) and bool(why_block) and has_options
    if is_structured_env or option_abort:
        # B (Phase 4 cost / autonomy): classify whether the install
        # path in option_a is low-risk enough that the orchestrator
        # could auto-resolve without paging the user. Surfaced as a
        # field; the actual "auto-resolve without asking" behavior is
        # gated by COS_AUTO_RESOLVE_ESCALATIONS env flag (default off)
        # and the UI uses this to render a "🚀 Auto-fix" suggestion.
        risk = _classify_install_risk(option_a)
        return {
            "kind": "environment",
            "question": text,
            "summary": escalation_header,
            "why": why_block,
            "option_a": option_a,
            "option_b": option_b,
            "option_abort": option_abort or "Cancel the task — env can't produce what was asked",
            "auto_resolvable": risk == "low",
            "risk": risk,
        }

    return {
        "kind": "general",
        "question": text,
        "option_a": option_a,
        "option_b": option_b,
    }


def _parse_skill_frontmatter(skill_md: str) -> tuple[str, str]:
    """Parse YAML-ish frontmatter from a SKILL.md. Returns (name, description)."""
    if not skill_md:
        return ("", "")
    lines = skill_md.splitlines()
    if not lines or not lines[0].strip().startswith("---"):
        return ("", "")
    name, desc = "", ""
    for line in lines[1:30]:
        s = line.strip()
        if s.startswith("---"):
            break
        if s.lower().startswith("name:"):
            name = s.split(":", 1)[1].strip()
        elif s.lower().startswith("description:"):
            desc = s.split(":", 1)[1].strip()
    return (name, desc)


def _detect_mcp_auth_need(tool_output: str) -> dict | None:
    """If a tool result indicates MCP needs OAuth/auth, extract the URL and return info.
    V3 hotfix: tightened to require BOTH a clear MCP-auth phrase AND an OAuth-shaped URL.
    Avoids false positives on workspace-internal URLs like http://10.0.2.2:5050/api/hello."""
    if not tool_output:
        return None
    low = tool_output.lower()
    strong_signals = (
        "open this url in their browser to authorize",
        "open this url in your browser to authorize",
        "ask the user to open this url",
        "complete the oauth flow",
        "authorize the plugin",
        "to authenticate this mcp server",
        "client_id=mcp_",
    )
    if not any(s in low for s in strong_signals):
        return None
    import re as _re
    url_match = _re.search(r"https?://[^\s\"']*(?:oauth|auth|authorize|authenticate)[^\s\"']*", tool_output, _re.IGNORECASE)
    if not url_match:
        return None
    return {"url": url_match.group(0), "snippet": tool_output[:400]}


def _read_hook_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except Exception:
        return []


class SupervisorLoop:
    def __init__(self, task: TaskState):
        self.task = task
        self.orchestrator = Orchestrator()
        self.reviewer = Reviewer()
        self.workspace = Path(task.workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.hook_log = LOG_ROOT / f"{task.id}.hook.jsonl"
        self.runner = ClaudeRunner(working_dir=self.workspace, hook_log_path=self.hook_log)
        self._action_count = 0
        self._review_pending: dict | None = None
        self._mcp_auth_requested: dict | None = None
        # B1: mid-stream coaching state. _on_event sets _mid_stream_coaching
        # when the reviewer flags a tool_use; the inner loop in run() reads
        # it after Claude exits, builds a coaching prompt, and re-spawns
        # with --resume <session_id>. _mid_stream_count is the per-outer-
        # loop counter; it's reset at the top of each correction loop.
        self._mid_stream_coaching: dict | None = None
        self._mid_stream_count = 0
        # B4: drift-detection state. _streak_non_reviewed counts consecutive
        # non-side-effecting tool_uses since the last reviewer pass; when it
        # crosses SELF_CHECK_AFTER_N_NON_REVIEWED we run a self-check.
        # _self_check_count is the per-outer-loop budget — reset alongside
        # _mid_stream_count at the top of each correction iteration.
        self._streak_non_reviewed = 0
        self._self_check_count = 0
        STORE.register_runner(task.id, self.runner)
        set_usage_callback(self._on_databricks_usage)
        # T7 (DOC3): record which prompt versions this task ran against
        # at task-start time. Each value is a 12-char content hash from
        # agents.prompts.prompt_version(). Persisted on the task row at
        # the next _persist call. The audit trail lets us answer
        # "what changed between task X (failed) and task Y (passed)?"
        # without re-deploying or guessing.
        try:
            from agents.prompts import prompt_version
            self.task.prompt_versions = {
                "orchestrator": prompt_version("orchestrator"),
                "reviewer": prompt_version("reviewer"),
            }
        except Exception:
            self.task.prompt_versions = {}

    def _build_coaching_prompt(self, coaching: dict) -> str:
        """Render the reviewer's mid-stream `correct` decision as a
        coaching message Claude reads on `--resume`. The flagged tool +
        input are echoed so Claude knows EXACTLY which action to avoid
        repeating."""
        tool = coaching.get("tool", "?")
        try:
            inp = json.dumps(coaching.get("input") or {}, default=str)[:400]
        except Exception:
            inp = str(coaching.get("input", ""))[:400]
        msg = (coaching.get("message") or "").strip()
        return (
            "Stop. The independent reviewer flagged your last action mid-stream:\n\n"
            f"  Tool: {tool}\n"
            f"  Input: {inp}\n\n"
            f"Reviewer says: {msg}\n\n"
            "Continue from where you left off, but address this issue. "
            "Do NOT repeat the flagged approach — pick a different path. "
            "After your fix, run the verification step and show its output."
        )

    async def _run_self_check(self, last_event: ClaudeEvent) -> None:
        """B4: drift check after a streak of non-side-effecting tool_uses.

        Runs the same Reviewer.review_action prompt the per-action gate uses,
        but with a synthetic tool name ``(self_check)`` so the reviewer sees
        this is a periodic trajectory audit rather than a specific action
        veto. Recent log is the actual signal — what did Claude actually do
        for the last N tool_uses?

        Elevation paths reuse B1's mid-stream plumbing:

          - ``escalate``  → set_escalation + runner.interrupt (mirrors B1)
          - ``correct``   → set _mid_stream_coaching + runner.interrupt; the
                            inner loop in run() will spawn a new Claude with
                            a coaching prompt that includes the reviewer's
                            message (same path B1 uses for tool flags)
          - ``approve``   → log only, keep going
          - ``request_evidence`` → append to corrections (deferred path)

        Failures are non-fatal — a drift-check is opportunistic, not load-
        bearing. If the Reviewer LLM 500s, log it and move on.
        """
        try:
            # #82: use drift_check (purpose-built prompt) instead of
            # shoehorning a synthetic tool_name into review_action — that
            # produced malformed JSON → silent default to 'approve' →
            # B4 shipped dark.
            # #83: hop onto a thread so we don't pause Claude during the
            # drift check.
            loop = asyncio.get_running_loop()
            review = await loop.run_in_executor(
                None,
                lambda: self.reviewer.drift_check(
                    goal=self.task.goal,
                    recent_actions=_summarize_log(self.task.log, limit=30),
                    workspace=str(self.workspace),
                    inline_skill="",
                ),
            )
        except Exception as e:
            review = {"decision": "approve", "message": f"self-check failed: {e}"}

        STORE.append_log(self.task.id, {
            "kind": "reviewer_self_check",
            "round": self._self_check_count,
            "decision": review["decision"],
            "message": review["message"],
        })

        if review["decision"] == "escalate":
            parsed = _parse_escalation(review["message"])
            STORE.set_escalation(self.task.id, parsed)
            self.runner.interrupt()
        elif review["decision"] == "correct":
            if (self._mid_stream_count < MAX_MID_STREAM_INTERRUPTS
                    and self._mid_stream_coaching is None):
                self._mid_stream_coaching = {
                    "tool": "(self_check)",
                    "input": {"trigger": "drift_check"},
                    "message": review["message"],
                }
                STORE.append_log(self.task.id, {
                    "kind": "mid_stream_coach_requested",
                    "tool": "(self_check)",
                    "round": self._mid_stream_count + 1,
                    "via": "self_check",
                })
                self.runner.interrupt()
            else:
                # Mid-stream cap hit OR coaching already pending — fall
                # through to the next correction loop with the message.
                self.task.corrections.append(review["message"])
        elif review["decision"] == "request_evidence":
            self.task.corrections.append(f"[evidence-needed] {review['message']}")

    def _on_databricks_usage(self, in_tokens: int, out_tokens: int,
                             caller: str | None = None,
                             system_chars: int = 0, user_chars: int = 0,
                             max_tokens: int = 0):
        # Per-call cost telemetry (post-Phase-4 cost analysis). Each
        # _chat() call lands here with its caller tag. We persist a
        # structured llm_call event so we can later run "what does
        # each phase cost" reports without re-running tasks.
        if caller:
            usd_in = (in_tokens / 1_000_000) * 5.0
            usd_out = (out_tokens / 1_000_000) * 25.0
            STORE.append_log(self.task.id, {
                "kind": "llm_call",
                "caller": caller,
                "in_tokens": in_tokens,
                "out_tokens": out_tokens,
                "system_chars": system_chars,
                "user_chars": user_chars,
                "max_tokens": max_tokens,
                "usd_estimate": round(usd_in + usd_out, 4),
            })
        # Audit 5-r bug fix (PHK3 hardening): also halt the task when
        # the Databricks token cap is breached. add_cost returns False
        # on either cap; we already enforce the Claude USD cap at the
        # claude_runner cost-write site below. This adds parity for
        # the orchestrator/reviewer LLM call path.
        if not STORE.add_cost(self.task.id, in_tokens=in_tokens, out_tokens=out_tokens):
            STORE.append_log(self.task.id, {
                "kind": "runaway_cost_stop",
                "reason": "databricks_tokens",
                "in_tokens": self.task.cost_databricks_in,
                "out_tokens": self.task.cost_databricks_out,
            })
            try:
                self.runner.interrupt()
            except Exception:
                pass

    def _should_nudge(self) -> bool:
        """Trigger a manager nudge only when: (a) >20 actions logged without a verification op,
        or (b) elapsed time on this task > 5 minutes."""
        actions = sum(1 for e in self.task.log if e.get("kind") == "tool_use")
        verify_kw = ("gradlew", "gradle", "pytest", "test ", "jest", "build", "assemble", "verify", "check", "lint")
        has_verify = any(
            (e.get("kind") == "tool_use") and any(k in (json.dumps(e.get("input") or {}).lower()) for k in verify_kw)
            for e in self.task.log
        )
        elapsed = time.time() - self.task.started_at
        return (actions > 20 and not has_verify) or elapsed > 300

    async def run(self):
        STORE.set_status(self.task.id, "skilling")
        STORE.append_log(self.task.id, {"kind": "phase", "phase": "generate_skill"})

        try:
            library_match = None
            try:
                library_match = rag.find_matching_skill(
                    self.task.goal + " " + (getattr(self.task, "skill_preview", "") or "")[:500],
                    distance_max=0.40,
                )
            except Exception as e:
                STORE.append_log(self.task.id, {"kind": "library_lookup_error", "msg": str(e)})

            if library_match:
                STORE.append_log(self.task.id, {
                    "kind": "library_match",
                    "matched_task_id": library_match.get("task_id"),
                    "distance": library_match.get("distance"),
                })

            # C2 (Phase 4 cost reduction, gated by COS_MERGED_BRIEF):
            # When the flag is set, ask the LLM for both SKILL.md and
            # the executor brief in a single call. Saves ~$0.05/task.
            # Opt-in until we A/B verify; if Claude didn't follow the
            # marker discipline, _split_merged_skill_brief returns
            # ('', '') and we fall through to the two-call path.
            #
            # Audit 6-r1 fix: accept "1" / "true" / "yes" / "on"
            # case-insensitive instead of strict "1" — footgun
            # avoidance, COS_MERGED_BRIEF=true was silently keeping
            # the slow path before.
            merged_brief: str | None = None
            _merged_flag = os.environ.get("COS_MERGED_BRIEF", "").strip().lower()
            if _merged_flag in ("1", "true", "yes", "on"):
                # The merged call needs env_audit context too — run it
                # early in this branch so both phases see it.
                try:
                    from services.env_audit import audit as _envaudit, render_brief_block as _envrender
                    _ea = _envaudit()
                    _eblock = _envrender(_ea)
                    STORE.append_log(self.task.id, {
                        "kind": "env_audit",
                        "available": [k for k, v in _ea.items() if v],
                        "missing": [k for k, v in _ea.items() if not v],
                        "phase": "merged_pre_brief",
                    })
                except Exception:
                    _eblock = ""
                try:
                    _ms, _mb = self.orchestrator.generate_skill_and_brief(
                        self.task.goal,
                        self.task.clarifications,
                        str(self.workspace),
                        skill_preview=getattr(self.task, "skill_preview", "") or "",
                        library_match=library_match,
                        env_audit=_eblock,
                    )
                    if _ms and _mb:
                        STORE.append_log(self.task.id, {
                            "kind": "merged_brief_used",
                            "skill_len": len(_ms), "brief_len": len(_mb),
                        })
                        skill_md = _ms
                        self.task.brief = _mb
                        merged_brief = _mb
                    else:
                        STORE.append_log(self.task.id, {
                            "kind": "merged_brief_fallback",
                            "reason": "missing ===SKILL=== or ===BRIEF=== markers",
                        })
                except Exception as _e:
                    STORE.append_log(self.task.id, {
                        "kind": "merged_brief_fallback",
                        "reason": f"exception: {_e}",
                    })

            if merged_brief is None:
                skill_md = self.orchestrator.generate_skill_brief(
                    self.task.goal,
                    self.task.clarifications,
                    skill_preview=getattr(self.task, "skill_preview", "") or "",
                    library_match=library_match,
                )
            save_task_skill(str(self.workspace), skill_md)
            self.task.skill_md = skill_md
            name, desc = _parse_skill_frontmatter(skill_md)
            self.task.skill_name = name
            self.task.skill_description = desc
            STORE.append_log(self.task.id, {
                "kind": "skill_generated",
                "preview": skill_md[:300],
                "refined_from_preview": bool(getattr(self.task, "skill_preview", "")),
                "library_match": bool(library_match),
                "skill_name": name,
                "skill_description": desc[:200],
            })
        except Exception as e:
            STORE.append_log(self.task.id, {"kind": "error", "where": "generate_skill", "msg": str(e)})
            STORE.set_status(self.task.id, "failed")
            self.task.result = {"success": False, "error": f"generate_skill failed: {e}"}
            return

        # G8: env audit before brief — probe the executor's machine for
        # available toolchains so the orchestrator can pivot the
        # deliverable shape if the obvious path is blocked (e.g. no
        # Android SDK → propose Expo Go QR instead). Total wall-time
        # ~400ms (parallel probes), worth the spend.
        # C2: skip when the merged path already produced the brief.
        if merged_brief is None:
            STORE.append_log(self.task.id, {"kind": "phase", "phase": "env_audit"})
            try:
                from services.env_audit import audit, render_brief_block
                env_audit_result = audit()
                env_audit_block = render_brief_block(env_audit_result)
                STORE.append_log(self.task.id, {
                    "kind": "env_audit",
                    "available": [k for k, v in env_audit_result.items() if v],
                    "missing": [k for k, v in env_audit_result.items() if not v],
                })
            except Exception as e:
                STORE.append_log(self.task.id, {"kind": "env_audit_failed", "msg": str(e)})
                env_audit_block = ""

            STORE.set_status(self.task.id, "briefing")
            STORE.append_log(self.task.id, {"kind": "phase", "phase": "build_brief"})

            try:
                self.task.brief = self.orchestrator.build_brief(
                    self.task.goal, self.task.clarifications, str(self.workspace),
                    inline_skill=self.task.skill_md or "",
                    env_audit=env_audit_block,
                )
            except Exception as e:
                STORE.append_log(self.task.id, {"kind": "error", "where": "build_brief", "msg": str(e)})
                STORE.set_status(self.task.id, "failed")
                self.task.result = {"success": False, "error": f"build_brief failed: {e}"}
                return
        else:
            # Merged path already produced both. Audit 6-r2 fix: emit
            # the same phase + brief log events that the two-call path
            # writes, so the dashboard / observability sees a complete
            # chronological trail. Status reflects briefing complete.
            STORE.append_log(self.task.id, {"kind": "phase", "phase": "build_brief"})
            STORE.set_status(self.task.id, "briefing")
            STORE.append_log(self.task.id, {
                "kind": "brief",
                "text": (self.task.brief or "")[:500],
                "via": "merged_call",
            })

        STORE.append_log(self.task.id, {"kind": "brief", "text": self.task.brief[:500]})

        # V3.5 #A1: detect DAG brief; if present, fan-out via LangGraph.
        try:
            steps = self.orchestrator.parse_dag(self.task.brief)
        except Exception:
            steps = None
        if steps and len(steps) > 1:
            STORE.append_log(self.task.id, {"kind": "dag_detected", "steps": [s["id"] for s in steps]})
            STORE.set_status(self.task.id, "executing_dag")
            hook_log_dir = self.workspace / "_hooks"

            def _bridge_step_event(step_id: str, ev):
                # Forward every Claude event from a parallel step into the
                # task's STORE log so SSE subscribers and /task/{id} polling
                # see live progress instead of a silent gap until the DAG ends.
                try:
                    payload = {"kind": "dag_step_event", "step_id": step_id}
                    et = getattr(ev, "type", None) or getattr(ev, "kind", None)
                    if et:
                        payload["event_type"] = et
                    text = getattr(ev, "text", None)
                    if text:
                        payload["text"] = text[:1000]
                    tool = getattr(ev, "tool", None)
                    if tool:
                        payload["tool"] = tool
                    STORE.append_log(self.task.id, payload)
                except Exception:
                    pass

            try:
                # V3.5 #3: build shared context from the brief's first
                # Objective/Deliverable sections so each parallel step
                # inherits the larger goal, not just its own one-line action.
                brief = self.task.brief or ""
                shared_ctx_parts: list[str] = [
                    f"OVERALL GOAL: {self.task.goal}",
                ]
                for hdr in ("## Objective", "## Deliverable", "## What needs to be built", "## Done"):
                    idx = brief.find(hdr)
                    if idx < 0:
                        continue
                    nxt = brief.find("\n## ", idx + 1)
                    excerpt = brief[idx: nxt if nxt > 0 else idx + 1200].strip()
                    shared_ctx_parts.append(excerpt[:1200])
                shared_context = "\n\n".join(shared_ctx_parts)

                dag_result = await dag_executor.execute_dag(
                    steps, self.workspace, hook_log_dir,
                    on_step_event=_bridge_step_event,
                    exec_id=self.task.id,
                    shared_context=shared_context,
                )
                STORE.append_log(self.task.id, {
                    "kind": "dag_result",
                    "ok": dag_result.get("ok"),
                    "failed": dag_result.get("failed", []),
                })
            except Exception as e:
                STORE.append_log(self.task.id, {"kind": "error", "where": "dag_executor", "msg": str(e)})
                dag_result = {"ok": False, "error": str(e)}

            # Drain each step's isolated hook log into the task log so
            # permission-hook events from every parallel step are preserved.
            for step_result in (dag_result.get("results") or {}).values():
                step_hook_log = step_result.get("hook_log")
                if not step_hook_log:
                    continue
                try:
                    for entry in _read_hook_log(Path(step_hook_log)):
                        STORE.append_log(self.task.id, {
                            "kind": "hook",
                            "step_id": step_result.get("step_id"),
                            **entry,
                        })
                except Exception:
                    pass

            STORE.set_status(self.task.id, "reviewing_loop_1")
            full_log = _summarize_log(self.task.log, limit=300, full_text=True)
            artifacts = _list_workspace_artifacts(self.workspace)
            # Approach A: parse the brief's declared deliverables and pass them
            # to the reviewer as ground truth. Approach B: also surface any
            # DELIVERABLE_PATHS marker Claude emitted at end-of-run.
            declared_paths = _parse_brief_deliverables(self.task.brief or "")
            executor_paths = _parse_executor_deliverable_marker(self.task.log)
            declared_block = _render_declared_deliverables(self.workspace, declared_paths)
            executor_block = _render_executor_declared(self.workspace, executor_paths)
            combined = "\n\n".join(filter(None, [
                full_log,
                f"DAG result: {dag_result}",
                f"=== Workspace artifacts ===\n{artifacts}",
                declared_block,
                executor_block,
            ]))
            review = self.reviewer.final_review(goal=self.task.goal, action_log=combined, workspace=str(self.workspace))
            STORE.append_log(self.task.id, {
                "kind": "final_review",
                "passed": review["passed"],
                "issues": review["issues"],
                "summary": review["summary"],
                "deliverables": review.get("deliverables", []),
            })
            if review["passed"] and not self.task.user_notes:
                # Same ordering fix — see audit r3 ordering note above.
                self.task.result = {
                    "success": True,
                    "summary": review["summary"],
                    "next_steps": review.get("next_steps", ""),
                    "deliverables": review.get("deliverables", []),
                    "loops": 1,
                    "execution": "dag_parallel",
                    "workspace": str(self.workspace),
                }
                STORE.set_status(self.task.id, "done")
                self._write_learning(1, review)
                self._index_in_rag(review)
                self._maybe_upload_trace(review)
                STORE.unregister_runner(self.task.id)
                return
            elif review["passed"]:
                # DAG passed BUT the user added notes mid-flight (e.g. "make
                # it orange" while the build was running). The DAG's
                # executor never saw those notes — they arrived after the
                # dispatcher split work. If we declare done now, the notes
                # are silently dropped. Force a correction loop so the
                # sequential executor can apply them.
                notes_preview = "; ".join(f'"{n[:80]}"' for n in self.task.user_notes[:3])
                self.task.corrections.append(
                    f"The DAG output passed verification but the user added "
                    f"{len(self.task.user_notes)} note(s) during execution that "
                    f"still need to be applied to the deliverable: {notes_preview}. "
                    f"Update the existing artifacts in place to incorporate them, "
                    f"verify the result still works, and surface the proof."
                )
                STORE.append_log(self.task.id, {
                    "kind": "dag_passed_but_notes_pending",
                    "notes": list(self.task.user_notes),
                    "action": "forcing_correction_loop_to_apply_notes",
                })
            else:
                # DAG failed review — fall through to normal correction loops
                self.task.corrections.extend(review["issues"])
                STORE.append_log(self.task.id, {"kind": "dag_review_failed_falling_through"})

            # V3.5 audit fix: notes added during executing_dag must not be
            # silently dropped — record them so the sequential fall-through
            # path consumes them, and so the user sees they were preserved.
            if self.task.user_notes:
                STORE.append_log(self.task.id, {
                    "kind": "notes_after_dag",
                    "notes": list(self.task.user_notes),
                    "applied": "queued_for_sequential_loop",
                })

        # Bug fix (live-task audit, Telegram task 20dbdac633e8):
        # When the DAG review FAILED and user added mid-flight notes,
        # the supervisor queued them via notes_after_dag and fell
        # through to sequential — but the initial sequential prompt was
        # just `self.task.brief`, so Claude never saw the notes on the
        # first turn. Notes sat in task.user_notes until a
        # correction-loop fail consumed them, which never happened in
        # the live run because loop_1 "passed" review (APK existed) but
        # the source code still said "Hello world" not "Hello Priyanka".
        # Inject any pending notes into the brief on first sequential
        # entry so Claude sees them on turn 1.
        prompt = self.task.brief
        if self.task.user_notes:
            notes_block = "\n".join(f"  - {n}" for n in self.task.user_notes[:10])
            prompt = (
                f"{self.task.brief}\n\n"
                "IMPORTANT — the user added these notes mid-flight while a previous\n"
                "DAG attempt was running. They MUST be applied to the deliverable\n"
                "in addition to the brief above:\n"
                f"{notes_block}\n\n"
                "If the brief and the notes conflict, the notes are more recent and win.\n"
                "When you finish, your DELIVERABLE_PATHS marker still names the same\n"
                "files; the difference is the file CONTENT must reflect the notes."
            )
            consumed = list(self.task.user_notes)
            self.task.user_notes.clear()
            STORE.append_log(self.task.id, {
                "kind": "notes_consumed",
                "notes": consumed,
                "consumed_at": "sequential_loop_entry",
            })
        session_id: str | None = None

        for loop_num in range(1, MAX_CORRECTION_LOOPS + 1):
            STORE.set_status(self.task.id, f"executing_loop_{loop_num}")
            STORE.append_log(self.task.id, {"kind": "phase", "phase": f"loop_{loop_num}"})

            # B1: each correction loop gets its own budget of mid-stream
            # interrupts. The inner while-loop below reuses the same
            # loop_num as long as we're just re-spawning Claude with
            # coaching; it falls through to final_review only when no
            # more coaching is requested (or the cap is hit).
            self._mid_stream_count = 0
            self._mid_stream_coaching = None
            # B4: reset drift state per correction loop too.
            self._streak_non_reviewed = 0
            self._self_check_count = 0
            inner_prompt = prompt

            while True:
                try:
                    result = await self.runner.run(
                        prompt=inner_prompt,
                        session_id=session_id,
                        on_event=self._on_event,
                    )
                except Exception as e:
                    STORE.append_log(self.task.id, {"kind": "error", "where": "claude_run", "msg": str(e)})
                    STORE.set_status(self.task.id, "failed")
                    self.task.result = {"success": False, "error": f"claude run failed: {e}"}
                    return

                # B1 #81: never overwrite a good session_id with None.
                # If runner.interrupt() fires before claude emits its
                # `system/init` event (e.g. reviewer flags the very first
                # tool, or SIGTERM races init), result.session_id is None.
                # Unconditional assignment would clobber the prior good id,
                # making the next --resume omit the flag entirely (since
                # _build_command only adds --resume on truthy session_id),
                # spawning a fresh Claude with no continuity. The coaching
                # prompt then says "Continue from where you left off" to a
                # session that has no "where".
                if result.session_id:
                    session_id = result.session_id

                # B1: did the reviewer ask us to coach mid-stream?
                if self._mid_stream_coaching:
                    coaching = self._mid_stream_coaching
                    self._mid_stream_coaching = None
                    self._mid_stream_count += 1
                    STORE.append_log(self.task.id, {
                        "kind": "mid_stream_resumed",
                        "loop": loop_num,
                        "round": self._mid_stream_count,
                        "tool": coaching.get("tool"),
                    })
                    inner_prompt = self._build_coaching_prompt(coaching)
                    # Replace the runner so /cancel and shutdown drain hit
                    # the latest live subprocess. The OLD runner has already
                    # exited (we got here only after .run() returned).
                    self.runner = ClaudeRunner(
                        working_dir=self.workspace,
                        hook_log_path=self.hook_log,
                    )
                    STORE.register_runner(self.task.id, self.runner)
                    continue

                break  # no mid-stream coach pending — fall through to review

            if result.cost_usd:
                # PHK3 (Phase 3.5 hardening): hard-stop the task when
                # cumulative spend exceeds COS_MAX_TASK_USD (default
                # $25). A stuck Claude could otherwise burn unbounded
                # spend silently. add_cost returns False when the cap
                # is breached.
                if not STORE.add_cost(self.task.id, claude_usd=float(result.cost_usd)):
                    STORE.append_log(self.task.id, {
                        "kind": "runaway_cost_stop",
                        "cost_usd": self.task.cost_claude_usd,
                    })
                    self.task.result = {
                        "success": False,
                        "error": (
                            f"Task halted: cumulative Claude spend "
                            f"${self.task.cost_claude_usd:.2f} exceeded "
                            f"COS_MAX_TASK_USD cap. "
                            f"Set COS_MAX_TASK_USD=0 to disable, or "
                            f"raise the cap to retry."
                        ),
                        "cost_claude_usd": self.task.cost_claude_usd,
                    }
                    STORE.set_status(self.task.id, "failed")
                    try:
                        self.runner.interrupt()
                    except Exception:
                        pass
                    STORE.unregister_runner(self.task.id)
                    return

            for entry in _read_hook_log(self.hook_log):
                STORE.append_log(self.task.id, {"kind": "hook", **entry})
            try:
                self.hook_log.write_text("")
            except Exception:
                pass

            if self.task.status == "escalated":
                resolved = await self._await_escalation()
                if resolved:
                    prompt = self._build_post_escalation_prompt()
                    continue
                else:
                    self.task.result = {"success": False, "error": "escalation unresolved"}
                    STORE.set_status(self.task.id, "failed")
                    return

            STORE.set_status(self.task.id, f"reviewing_loop_{loop_num}")
            full_log = _summarize_log(self.task.log, limit=300, full_text=True)
            artifacts = _list_workspace_artifacts(self.workspace)
            # Approach A + B (audit r3 deeper-fix): pass brief-declared and
            # executor-declared deliverables as ground truth so the reviewer
            # doesn't have to infer what was the deliverable from a sea of
            # workspace artifacts.
            declared_paths = _parse_brief_deliverables(self.task.brief or "")
            executor_paths = _parse_executor_deliverable_marker(self.task.log)
            declared_block = _render_declared_deliverables(self.workspace, declared_paths)
            executor_block = _render_executor_declared(self.workspace, executor_paths)
            combined = "\n\n".join(filter(None, [
                full_log,
                f"=== Workspace artifacts (actual files) ===\n{artifacts}",
                declared_block,
                executor_block,
            ]))
            review = self.reviewer.final_review(
                goal=self.task.goal,
                action_log=combined,
                workspace=str(self.workspace),
                inline_skill="",
            )
            STORE.append_log(self.task.id, {
                "kind": "final_review",
                "passed": review["passed"],
                "issues": review["issues"],
                "summary": review["summary"],
                "deliverables": review.get("deliverables", []),
            })

            # Bug fix (live-task audit, Telegram task 20dbdac633e8):
            # mirror the DAG path's "passed=True AND no pending user_notes"
            # check. If the user added a note mid-loop and the loop
            # finished without consuming it, force another correction
            # loop instead of declaring done — otherwise we ship a
            # deliverable that ignores the user's most recent ask.
            if review["passed"] and self.task.user_notes:
                notes_preview = "; ".join(f'"{n[:80]}"' for n in self.task.user_notes[:3])
                self.task.corrections.append(
                    f"Review passed but {len(self.task.user_notes)} user note(s) "
                    f"are still pending and were NOT applied to the deliverable: "
                    f"{notes_preview}. Update the deliverable in place to reflect "
                    f"these notes, then re-verify."
                )
                STORE.append_log(self.task.id, {
                    "kind": "loop_passed_but_notes_pending",
                    "loop": loop_num,
                    "notes": list(self.task.user_notes),
                    "action": "forcing_correction_loop_to_apply_notes",
                })
                # Fall through to the post-pass correction-prep block
                # below by skipping the success early-return.
            elif review["passed"]:
                # Bug fix (Phase 3 audit r3, ordering): assign result
                # BEFORE set_status. set_status calls _persist; if the
                # result is set after, the DB row keeps result=NULL and
                # post-restart hydration loses the deliverables list +
                # summary. Same fix at the DAG-pass and best-effort
                # paths below.
                self.task.result = {
                    "success": True,
                    "summary": review["summary"],
                    "next_steps": review.get("next_steps", ""),
                    "deliverables": review.get("deliverables", []),
                    "loops": loop_num,
                    "corrections_made": len(self.task.corrections),
                    "workspace": str(self.workspace),
                }
                STORE.set_status(self.task.id, "done")
                self._write_learning(loop_num, review)
                self._index_in_rag(review)
                STORE.unregister_runner(self.task.id)
                self._maybe_upload_trace(review)
                return

            self.task.corrections.extend(review["issues"])
            STORE.append_log(self.task.id, {"kind": "correction", "issues": review["issues"]})

            user_notes = list(self.task.user_notes)
            if user_notes:
                self.task.user_notes.clear()
                STORE.append_log(self.task.id, {"kind": "notes_consumed", "notes": user_notes})

            if loop_num == MAX_CORRECTION_LOOPS:
                # Same ordering fix — see audit r3 ordering note above.
                self.task.result = {
                    "success": False,
                    "best_effort": True,
                    "summary": review["summary"],
                    "issues": review["issues"],
                    "deliverables": review.get("deliverables", []),
                    "loops": loop_num,
                    "workspace": str(self.workspace),
                }
                STORE.set_status(self.task.id, "failed")
                self._write_learning(loop_num, review)
                self._index_in_rag(review)
                STORE.unregister_runner(self.task.id)
                return

            grounding = ""
            if self._should_nudge():
                try:
                    grounding = self.orchestrator.generate_grounding_nudge(
                        task=self.task.goal,
                        brief=self.task.brief,
                        action_log_summary=_summarize_log(self.task.log, limit=60),
                        loop_num=loop_num,
                    )
                    if grounding:
                        STORE.append_log(self.task.id, {"kind": "grounding_nudge", "nudge": grounding})
                except Exception as e:
                    STORE.append_log(self.task.id, {"kind": "grounding_error", "msg": str(e)})

            prompt = self.orchestrator.build_correction_prompt(
                self.task.brief, review["issues"],
                user_notes=user_notes,
                grounding_nudge=grounding,
            )

    def _maybe_inject_midloop_nudge(self):
        """V3 #8: mid-loop grounding. Every 25 tool_use events without a verification op,
        synthesize a nudge and append to corrections (will be folded into next loop's prompt
        OR — when we add true interrupt — injected directly mid-flight)."""
        actions = sum(1 for e in self.task.log if e.get("kind") == "tool_use")
        if actions == 0 or actions % 25 != 0:
            return
        recent_text = " ".join(json.dumps(e.get("input") or {}).lower() for e in self.task.log[-25:] if e.get("kind") == "tool_use")
        if any(k in recent_text for k in ("gradle", "pytest", "test ", "build", "verify", "check")):
            return
        try:
            nudge = self.orchestrator.generate_grounding_nudge(
                task=self.task.goal,
                brief=self.task.brief,
                action_log_summary=_summarize_log(self.task.log, limit=25),
                loop_num=0,
            )
            if nudge:
                self.task.corrections.append(f"[mid-loop nudge] {nudge}")
                STORE.append_log(self.task.id, {"kind": "midloop_nudge", "nudge": nudge[:300]})
        except Exception:
            pass

    async def _on_event(self, event: ClaudeEvent):
        if event.type == "init":
            STORE.append_log(self.task.id, {"kind": "init", "session_id": event.session_id})
            announced = (event.raw or {}).get("tools") or []
            if announced:
                review, skip = classify_announced_tools(announced)
                STORE.append_log(self.task.id, {
                    "kind": "tools_classified",
                    "review_count": len(review),
                    "review": review,
                    "skip_count": len(skip),
                })
            return

        if event.type == "text":
            STORE.append_log(self.task.id, {"kind": "text", "text": event.text or ""})
            # G7+: scan text events for the ESCALATION:/WHY:/OPTIONS:
            # marker the orchestrator brief tells Claude to emit on
            # env walls. If found, parse + surface as an environment
            # escalation and interrupt — beats waiting for Claude to
            # keep grinding.
            if event.text and not self.task.escalation:
                if re.search(r"^\s*[\*\-]?\s*ESCALATION\s*:", event.text, re.IGNORECASE | re.MULTILINE):
                    parsed = _parse_escalation(event.text)
                    if parsed.get("kind") == "environment":
                        STORE.set_escalation(self.task.id, parsed)
                        STORE.append_log(self.task.id, {
                            "kind": "executor_escalated",
                            "summary": parsed.get("summary", "")[:200],
                        })
                        try:
                            self.runner.interrupt()
                        except Exception:
                            pass
            return

        if event.type == "tool_use":
            self._action_count += 1
            # T7 (STREAM-TIME): increment per-task Claude turn count so
            # we can later compare "200-turn loop" pathological tasks
            # against typical "12-turn quick task" runs. Persisted at
            # the next _persist on terminal status.
            self.task.claude_turn_count += 1
            STORE.append_log(self.task.id, {
                "kind": "tool_use",
                "tool": event.tool_name,
                "input": event.tool_input,
            })
            self._maybe_inject_midloop_nudge()

            if event.tool_name == "TodoWrite":
                todos = (event.tool_input or {}).get("todos") or []
                if todos:
                    self.task.claude_plan = todos
                    STORE.append_log(self.task.id, {
                        "kind": "plan_updated",
                        "items": [{"content": t.get("content",""), "status": t.get("status","")} for t in todos],
                    })

            if is_side_effecting(event.tool_name):
                # B4: any side-effecting tool already triggers per-action
                # review below — that resets the drift streak.
                self._streak_non_reviewed = 0
                try:
                    # #83: hop the synchronous Databricks call onto a
                    # thread so this coroutine doesn't pause Claude's
                    # stdout drain (and therefore Claude itself once the
                    # OS pipe fills).
                    loop = asyncio.get_running_loop()
                    review = await loop.run_in_executor(
                        None,
                        lambda: self.reviewer.review_action(
                            goal=self.task.goal,
                            tool_name=event.tool_name or "",
                            tool_input=event.tool_input,
                            recent_actions=_summarize_log(self.task.log, limit=20),
                            workspace=str(self.workspace),
                            inline_skill="",
                        ),
                    )
                except Exception as e:
                    review = {"decision": "approve", "message": f"review failed: {e}"}

                STORE.append_log(self.task.id, {
                    "kind": "reviewer",
                    "tool": event.tool_name,
                    "decision": review["decision"],
                    "message": review["message"],
                })

                if review["decision"] == "escalate":
                    parsed = _parse_escalation(review["message"])
                    STORE.set_escalation(self.task.id, parsed)
                    self.runner.interrupt()
                elif review["decision"] == "correct":
                    # B1: try mid-stream interrupt FIRST (immediate coach via
                    # --resume); fall back to deferred-to-next-loop only
                    # when we've hit the cap or another coach is already
                    # queued for this Claude exit.
                    if (self._mid_stream_count < MAX_MID_STREAM_INTERRUPTS
                            and self._mid_stream_coaching is None):
                        self._mid_stream_coaching = {
                            "tool": event.tool_name,
                            "input": event.tool_input,
                            "message": review["message"],
                        }
                        STORE.append_log(self.task.id, {
                            "kind": "mid_stream_coach_requested",
                            "tool": event.tool_name,
                            "round": self._mid_stream_count + 1,
                        })
                        self.runner.interrupt()
                    else:
                        # Cap hit OR a coach is already pending — defer to
                        # the next correction loop's prompt as before.
                        self.task.corrections.append(review["message"])
                elif review["decision"] == "request_evidence":
                    self.task.corrections.append(f"[evidence-needed] {review['message']}")
            else:
                # B4: drift check. The current tool isn't side-effecting,
                # so the per-action gate didn't fire. Track the streak;
                # once it hits the threshold, run a self-check pass.
                self._streak_non_reviewed += 1
                if (self._streak_non_reviewed >= SELF_CHECK_AFTER_N_NON_REVIEWED
                        and self._self_check_count < MAX_SELF_CHECKS_PER_LOOP
                        and self._mid_stream_coaching is None):
                    self._streak_non_reviewed = 0
                    self._self_check_count += 1
                    await self._run_self_check(event)
            return

        if event.type == "tool_result":
            STORE.append_log(self.task.id, {
                "kind": "tool_result",
                "output": event.tool_output or "",
                "is_error": event.is_error,
            })
            mcp_auth = _detect_mcp_auth_need(event.tool_output or "")
            if mcp_auth and not self._mcp_auth_requested:
                self._mcp_auth_requested = mcp_auth
                STORE.set_escalation(self.task.id, {
                    "question": (
                        f"Claude needs to authenticate an MCP tool to continue. "
                        f"Open this URL to authorize:\n\n{mcp_auth['url']}\n\n"
                        f"After authorizing, reply A. To skip the MCP and have Claude use built-in knowledge instead, reply B."
                    ),
                    "option_a": f"I authorized — please retry the MCP call",
                    "option_b": f"Skip the MCP, use built-in knowledge / fallback approach",
                })
                self.runner.interrupt()
            return

        if event.type == "result":
            STORE.append_log(self.task.id, {
                "kind": "result",
                "text": event.text or "",
                "is_error": event.is_error,
            })
            return

    async def _await_escalation(self) -> bool:
        try:
            await asyncio.wait_for(
                self.task.escalation_event.wait(),
                timeout=ESCALATION_AUTO_RESOLVE_SECS,
            )
            STORE.append_log(self.task.id, {
                "kind": "escalation_resolved",
                "answer": self.task.escalation_answer,
                "via": "user",
            })
            return True
        except asyncio.TimeoutError:
            esc = self.task.escalation or {}
            answer = self.orchestrator.auto_resolve_escalation(
                goal=self.task.goal,
                brief=self.task.brief,
                action_log_summary=_summarize_log(self.task.log, limit=40),
                question=esc.get("question", ""),
                option_a=esc.get("option_a", ""),
                option_b=esc.get("option_b", ""),
            )
            self.task.escalation_answer = answer
            STORE.append_log(self.task.id, {
                "kind": "escalation_resolved",
                "answer": answer,
                "via": "auto_orchestrator",
            })
            return True

    def _maybe_upload_trace(self, review: dict):
        """V3 #11: opt-in anonymized trace upload. Only fires if TRACE_UPLOAD_URL is set."""
        import os as _os
        url = _os.environ.get("TRACE_UPLOAD_URL", "").strip()
        if not url:
            return
        try:
            import urllib.request as _ur, urllib.error as _ue
            payload = {
                "task_id_hash": __import__("hashlib").sha256(self.task.id.encode()).hexdigest()[:16],
                "goal": self.task.goal,
                "loops": self.task.loop_count if hasattr(self.task, "loop_count") else None,
                "passed": review.get("passed", False),
                "summary": review.get("summary", ""),
                "skill_md": self.task.skill_md,
                "cost_in": self.task.cost_databricks_in,
                "cost_out": self.task.cost_databricks_out,
            }
            data = __import__("json").dumps(payload).encode()
            req = _ur.Request(url, data=data, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_os.environ.get('TRACE_UPLOAD_TOKEN','')}",
            })
            _ur.urlopen(req, timeout=5).read()
            STORE.append_log(self.task.id, {"kind": "trace_uploaded"})
        except Exception as e:
            STORE.append_log(self.task.id, {"kind": "trace_upload_error", "msg": str(e)})

    def _index_in_rag(self, review: dict):
        try:
            summary = review.get("summary", "") or ""
            rag.index_task(
                task_id=self.task.id,
                goal=self.task.goal,
                summary=summary,
                skill_md=self.task.skill_md or "",
            )
            if self.task.skill_description:
                rag.index_skill(
                    task_id=self.task.id,
                    name=self.task.skill_name,
                    description=self.task.skill_description,
                    skill_md=self.task.skill_md or "",
                )
            STORE.append_log(self.task.id, {"kind": "rag_indexed"})
        except Exception as e:
            STORE.append_log(self.task.id, {"kind": "rag_index_error", "msg": str(e)})

        # T4: also persist a structured memory to Mem0 cloud so
        # future tasks can pick up cross-task context (user prefs,
        # project facts, recurring patterns). No-op when MEM0_API_KEY
        # is unset — pgvector + skill_lessons remain authoritative.
        try:
            from services import memory as mem
            if mem.is_enabled():
                # Single-user system today; if/when we add multi-user,
                # plumb the originating user_id through TaskState.
                user_id = "cos-default"
                deliverables = []
                if isinstance(self.task.result, dict):
                    deliverables = self.task.result.get("deliverables") or []
                ok = mem.add_task_memory(
                    task=self.task.goal,
                    deliverables=deliverables,
                    summary=review.get("summary", "") or "",
                    rationale=review.get("rationale", "") or "",
                    user_id=user_id,
                    metadata={"task_id": self.task.id, "passed": bool(review.get("passed"))},
                )
                STORE.append_log(self.task.id, {
                    "kind": "mem0_indexed" if ok else "mem0_skipped",
                })
        except Exception as e:
            STORE.append_log(self.task.id, {"kind": "mem0_index_error", "msg": str(e)})

    def _write_learning(self, loop_num: int, review: dict):
        try:
            lessons = self.orchestrator.find_promotable_lessons(
                task=self.task.goal,
                skill_md=getattr(self.task, "skill_md", "") or "",
                brief=self.task.brief,
                action_log_summary=_summarize_log(self.task.log, limit=80),
                review_summary=review.get("summary", ""),
                review_issues=review.get("issues", []) or [],
                passed=bool(review.get("passed")),
                workspace=str(self.workspace),
            )
            if not lessons:
                STORE.append_log(self.task.id, {"kind": "learning_skipped", "reason": "no promotable lessons"})
                return
            added = append_to_global(lessons, origin_task_id=self.task.id)
            STORE.append_log(self.task.id, {
                "kind": "learning_promoted",
                "added_new": added,
                "promoted_total": len(lessons),
                "lessons": lessons,
            })
        except Exception as e:
            STORE.append_log(self.task.id, {"kind": "learning_error", "msg": str(e)})

    def _build_post_escalation_prompt(self) -> str:
        """V3.5 D5: handle free-text escalation answers in addition to a/b.

        - 'a' / 'b' (legacy): pick option_a / option_b from the escalation dict.
        - Anything else: treat as a free-text directive from the user
          ('do X instead', 'try Y', 'use lib Z'). Inject verbatim — the user
          knows their own context better than our orchestrator."""
        esc = self.task.escalation or {}
        ans = (self.task.escalation_answer or "").strip()
        STORE.set_status(self.task.id, "executing")
        if ans.lower() == "a":
            chosen = esc.get("option_a") or "(option A)"
            decision_block = f"The decision is: {chosen}"
        elif ans.lower() == "b":
            chosen = esc.get("option_b") or "(option B)"
            decision_block = f"The decision is: {chosen}"
        else:
            decision_block = (
                "The user gave a free-text directive (treat as authoritative — "
                "the user knows their context better than the reviewer):\n"
                f"  > {ans}"
            )
        return (
            "Continue the original task. The reviewer paused you with a question.\n\n"
            f"{decision_block}\n\n"
            f"Original brief:\n{self.task.brief}\n\n"
            "Resume the work using this decision."
        )


async def run_task(task_id: str):
    state = STORE.get(task_id)
    if not state:
        return
    loop = SupervisorLoop(state)
    try:
        await loop.run()
    except Exception as e:
        STORE.append_log(task_id, {"kind": "fatal", "msg": str(e)})
        STORE.set_status(task_id, "failed")
        state.result = {"success": False, "error": f"loop crashed: {e}"}
