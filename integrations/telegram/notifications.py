"""Outbound notifications: action-line preview, status polling, escalation
prompts, and completion summaries.

These run as fire-and-forget asyncio tasks scheduled by the conversation
handlers. The contract is "given a chat_id and a task_id, ping the user
when something interesting happens" — escalations, completion, status
transitions worth surfacing.

Why polling and not server-push: the Telegram bot runs as a separate
process and talks to the FastAPI server over HTTP. Long polling against
GET /task/{id} is simpler than wiring SSE through python-telegram-bot's
event loop, and the polling interval is cheap (5s) for the few in-flight
tasks a single user has at once.
"""
import asyncio
import logging

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import SUPERVISOR_API_BASE_URL

from .auth import HTTP_TIMEOUT, POLLING_INTERVAL_SECS


log = logging.getLogger(__name__)


async def _send_action_line(context: ContextTypes.DEFAULT_TYPE, chat_id: int, task_id: str):
    """Once the orchestrator has generated the per-task SKILL.md, send
    ONE line to the user telling them what's about to happen. Polls
    GET /task/{id} up to 60 times (≈2 minutes) waiting for skill_md to
    land; gives up silently if the task finishes before the skill is
    materialized."""
    for _ in range(60):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{SUPERVISOR_API_BASE_URL}/task/{task_id}",
                    timeout=HTTP_TIMEOUT,
                )
                r.raise_for_status()
                state = r.json()
        except Exception:
            await asyncio.sleep(2)
            continue
        skill_md = state.get("skill_md", "")
        if skill_md:
            line = _extract_action_line(skill_md)
            if line:
                await context.bot.send_message(chat_id, f"What I'll do: {line}")
            return
        if state.get("status") in ("done", "failed", "abandoned"):
            return
        await asyncio.sleep(2)


def _extract_action_line(skill_md: str) -> str:
    """Pull a single one-sentence action statement from the SKILL.md
    Objective section. Returns empty string when the section is missing
    or empty — caller treats that as 'no preview, skip the message'."""
    if not skill_md:
        return ""
    lines = skill_md.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("## objective"):
            for follow in lines[i + 1:i + 8]:
                s = follow.strip()
                if not s or s.startswith("#"):
                    continue
                first_sentence = s.split(". ")[0].rstrip(".") + "."
                first_sentence = first_sentence.replace("**", "")
                return first_sentence[:300]
    return ""


async def _poll_task(context: ContextTypes.DEFAULT_TYPE, chat_id: int, task_id: str):
    """Long-poll the supervisor's /task/{id} endpoint until terminal.
    Surfaces interesting status transitions (correction loops, reviews)
    and dispatches to _send_escalation / _send_completion when those
    events fire."""
    last_status: str | None = None
    # Bug #4 fix: surface DAG step transitions so the user isn't staring
    # at a silent chat for 5+ minutes during a multi-step build (Android
    # gradle, RN expo export, etc.). We announce when each step starts
    # and finishes, but only once — no spam on every poll.
    announced_started: set[str] = set()
    announced_done: set[str] = set()
    while True:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{SUPERVISOR_API_BASE_URL}/task/{task_id}",
                    timeout=HTTP_TIMEOUT,
                )
                r.raise_for_status()
                state = r.json()
        except Exception as e:
            log.warning("poll failed: %s", e)
            await asyncio.sleep(POLLING_INTERVAL_SECS)
            continue

        status = state.get("status", "")

        if status != last_status:
            last_status = status
            if status.startswith("reviewing_"):
                await context.bot.send_message(
                    chat_id,
                    f"🔍 Reviewing {status.replace('reviewing_', '')}...",
                )
            elif status.startswith("executing_loop_") and status != "executing_loop_1":
                loop_n = status.split("_")[-1]
                await context.bot.send_message(
                    chat_id,
                    f"🔄 Starting correction loop {loop_n}...",
                )
            elif status == "executing_dag":
                await context.bot.send_message(
                    chat_id,
                    "🛠️ Running parallel build steps — I'll narrate progress as steps land.",
                )

        # Bug #4 fix: announce DAG step transitions even when the umbrella
        # status doesn't change. dag_progress is a {step_id: {count, last}}
        # map; "started" = first time we see it, "done" = a 'result' event.
        dag_progress = state.get("dag_progress") or {}
        for step_id, p in dag_progress.items():
            if step_id not in announced_started and p.get("count", 0) > 0:
                announced_started.add(step_id)
                last_text = (p.get("last") or "").strip().split("\n")[0][:140]
                tail = f" — {last_text}" if last_text else ""
                await context.bot.send_message(
                    chat_id, f"▶️ Step `{step_id}` started{tail}",
                )
        # Find dag_step_event entries with event_type=result for completion.
        for e in (state.get("log_tail") or []):
            if (e.get("kind") == "dag_step_event"
                    and e.get("event_type") == "result"
                    and e.get("step_id")
                    and e["step_id"] not in announced_done):
                announced_done.add(e["step_id"])
                tail = (e.get("text") or "").strip().split("\n")[0][:140]
                tailmsg = f" — {tail}" if tail else ""
                await context.bot.send_message(
                    chat_id, f"✅ Step `{e['step_id']}` completed{tailmsg}",
                )

        if status == "escalated":
            esc = state.get("escalation") or {}
            await _send_escalation(context, chat_id, task_id, esc)
            return

        if status in ("done", "failed", "abandoned"):
            await _send_completion(context, chat_id, task_id, state)
            return

        await asyncio.sleep(POLLING_INTERVAL_SECS)


async def _send_escalation(context, chat_id: int, task_id: str, esc: dict):
    """Render an A/B inline keyboard for an escalation. The callback_data
    encodes the task_id + answer letter so the callback handler can
    submit it back to /task/{id}/escalation without keeping per-chat
    state."""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("A", callback_data=f"esc:{task_id}:a"),
        InlineKeyboardButton("B", callback_data=f"esc:{task_id}:b"),
    ]])
    text = (
        f"🔴 *Need your decision* (`{task_id}`)\n\n"
        f"{esc.get('question', '(no question)')}\n\n"
        f"*A)* {esc.get('option_a', '')}\n"
        f"*B)* {esc.get('option_b', '')}"
    )
    await context.bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")


async def _send_completion(context, chat_id: int, task_id: str, state: dict):
    """Final summary message at task end. Includes the reviewer's
    next_steps when present so the user knows EXACTLY how to use the
    deliverable, without having to ssh into the workspace and figure it
    out themselves."""
    status = state["status"]
    result = state.get("result") or {}
    duration = round(state.get("duration_secs", 0), 1)
    workspace = state.get("workspace", "?")
    emoji = {"done": "✅", "failed": "❌", "abandoned": "⚠️", "cancelled": "⚪"}.get(status, "❓")
    summary = result.get("summary") or "(no summary)"
    next_steps = result.get("next_steps") or ""

    text = f"{emoji} Task {task_id} — {status} in {duration:.0f}s\n\n{summary[:1500]}"

    if next_steps:
        text += f"\n\n— How to use it —\n{next_steps[:2500]}"

    if not result.get("success") and result.get("issues"):
        issues_text = "\n".join(f"• {i[:200]}" for i in (result.get("issues") or [])[:3])
        text += f"\n\nIssues:\n{issues_text}"

    text += f"\n\nWorkspace: {workspace}"

    await context.bot.send_message(chat_id, text)

    # Bug #1 fix: send the actual deliverable artifacts back in chat.
    # User explicitly asked "send it to me here" — text alone isn't enough.
    await _send_artifacts(context, chat_id, state)


_ARTIFACT_NOISE_DIRS = ("node_modules/", "_hooks/", "dist/", ".git/", "venv/",
                        "__pycache__/", ".gradle/", ".idea/", "build/intermediates/",
                        "Pods/", "DerivedData/", "gradle/wrapper/")

# Build / scaffolding files that are technically at workspace root but are
# never the user's deliverable. The user asked for an APK — they don't
# want gradlew or .gitignore.
_ARTIFACT_NOISE_NAMES = frozenset({
    "gradlew", "gradlew.bat", "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts", "gradle.properties",
    "local.properties", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "bun.lockb", "Cargo.lock", "Pipfile.lock", "poetry.lock",
    "babel.config.js", "metro.config.js", "tsconfig.json", "jsconfig.json",
    ".gitignore", ".npmignore", ".eslintrc.js", ".prettierrc",
    "Gemfile", "Gemfile.lock", "Podfile", "Podfile.lock",
    "Dockerfile", "docker-compose.yml", "Makefile",
})

_TG_DOC_MAX_BYTES = 49 * 1024 * 1024  # 1MB headroom under TG's 50MB cap


def _is_user_facing_artifact(path: str) -> bool:
    if any(noise in path for noise in _ARTIFACT_NOISE_DIRS):
        return False
    name = path.rsplit("/", 1)[-1]
    if name in _ARTIFACT_NOISE_NAMES:
        return False
    return True


async def _send_artifacts(context, chat_id: int, state: dict):
    """Send the deliverable artifacts the user actually asked for —
    nothing else.

    Selection rule (strict, deliberately narrow):
      1. If any artifact's filename appears verbatim in result.summary or
         next_steps (the reviewer's distilled "what got produced"), send
         ONLY those. The reviewer-named file IS the deliverable.
      2. Else if the summary references a file extension (`.apk`, `.pdf`,
         `.zip`, `.md`, `.csv`, …), send only artifacts at workspace root
         matching that extension.
      3. Else fall back to root-level user-facing files (deduplicated by
         basename so we don't send `hello-world-debug.apk` AND its
         build-output twin `app/build/outputs/apk/debug/app-debug.apk`).

    Hard caps: ≤3 documents per task, ≤49MB per file, drop scaffolding
    (gradlew, package-lock, .gitignore, …) at any tier.
    """
    import os
    import re
    workspace = state.get("workspace") or ""
    result = state.get("result") or {}
    artifacts = state.get("artifacts") or []
    if not workspace or not artifacts:
        return

    summary_text = (result.get("summary") or "") + " " + (result.get("next_steps") or "")
    summary_lower = summary_text.lower()

    user_facing = [a for a in artifacts
                   if a.get("path") and _is_user_facing_artifact(a["path"])
                   and 0 < (a.get("size_bytes") or 0) <= _TG_DOC_MAX_BYTES]

    # Tier 1: filename verbatim in summary.
    named = [a for a in user_facing
             if (a["path"].rsplit("/", 1)[-1]).lower() in summary_lower]

    # Tier 2: extension referenced in summary, e.g. ".apk", ".pdf".
    if not named:
        ext_hits = re.findall(r"\.[a-z0-9]{2,5}\b", summary_lower)
        wanted_exts = {e for e in ext_hits if e not in (".com", ".net", ".org",
                                                        ".io", ".md")}
        # ".md" is allowed if explicitly the deliverable, but otherwise it
        # picks up README noise — only honour it if it's the ONLY ext.
        if not wanted_exts and ".md" in ext_hits:
            wanted_exts = {".md"}
        if wanted_exts:
            named = [a for a in user_facing
                     if "/" not in a["path"]
                     and any(a["path"].lower().endswith(e) for e in wanted_exts)]

    # Tier 3: root-level user-facing files, deduplicated by basename.
    if not named:
        seen_basenames = set()
        named = []
        for a in user_facing:
            if "/" in a["path"]:
                continue
            b = a["path"].rsplit("/", 1)[-1].lower()
            if b in seen_basenames:
                continue
            seen_basenames.add(b)
            named.append(a)

    # Sort descending by size so the actual deliverable beats README on
    # ties; cap at 3.
    named.sort(key=lambda a: -(a.get("size_bytes") or 0))
    top = named[:3]

    sent = 0
    for a in top:
        abs_path = a.get("abs_path") or os.path.join(workspace, a["path"])
        try:
            with open(abs_path, "rb") as fh:
                await context.bot.send_document(
                    chat_id,
                    document=fh,
                    filename=a["path"].rsplit("/", 1)[-1],
                    caption=f"📎 {a['path']}",
                )
            sent += 1
        except Exception as e:
            log.warning("send_document(%s) failed: %s", abs_path, e)

    skipped_oversize = sum(1 for a in artifacts
                           if a.get("path") and _is_user_facing_artifact(a["path"])
                           and (a.get("size_bytes") or 0) > _TG_DOC_MAX_BYTES)
    extras = max(0, len(named) - sent)
    if sent and (extras or skipped_oversize):
        notes = []
        if extras:
            notes.append(f"+{extras} more deliverable file(s) in workspace")
        if skipped_oversize:
            notes.append(f"{skipped_oversize} file(s) > 50MB (Telegram limit)")
        await context.bot.send_message(chat_id, " · ".join(notes))
