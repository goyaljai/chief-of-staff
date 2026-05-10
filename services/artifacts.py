"""Workspace artifact helpers — extracted from main.py during the v2.0 refactor.

Two pure helpers used by:
  • GET /task/{id}      — surface artifacts in the response payload
  • POST /task/{id}/ask — embed artifact previews in the answer prompt
  • supervisor_loop      — gather workspace state for review

Why a separate file:
  - Both functions are stateless utilities (no global state, no DB).
  - They don't belong inside the route handler module — they're orthogonal
    to HTTP and reused from supervisor_loop.
  - Keeping them here means the artifact policy (which dirs to skip, which
    deliverables to surface, preview byte caps) lives in one place.

Public exports:
  list_artifacts(workspace, max_files=30, max_preview=3000) -> list[dict]
  last_claude_output(log) -> str
"""
from pathlib import Path


# Binary deliverables we WANT to surface (with metadata, no preview content)
# even though they're inside skip_dirs like build/. These are user-visible
# release artifacts — APKs, JARs, wheels, archives.
DELIVERABLE_BINARY_EXTS = (
    ".apk", ".ipa", ".jar", ".aar",
    ".zip", ".tar.gz", ".tgz", ".whl",
    ".dmg", ".pkg",
)


def list_artifacts(
    workspace: str,
    max_files: int = 30,
    max_preview: int = 3000,
) -> list[dict]:
    """List user-facing artifact files in the workspace.

    Skips directories that are build noise (`node_modules`, `.gradle`,
    `__pycache__`, etc.) EXCEPT when they contain high-value binary
    deliverables (APK, JAR, etc.) — those are surfaced with metadata only,
    no content preview.

    Returns a list of dicts:
        {path, abs_path, size_bytes, preview}
    where `preview` is up to `max_preview` chars of text content, or a
    placeholder string for binary files.

    Sorted by mtime descending — most-recently-touched first. Capped at
    `max_files` to keep response payloads reasonable.
    """
    if not workspace:
        return []
    p = Path(workspace)
    if not p.exists():
        return []

    skip_dirs = {
        "skills", ".claude", ".gradle", ".idea", "build",
        "node_modules", "__pycache__", "venv", ".venv", "intermediates",
    }

    out: list[dict] = []
    deliverables: list[dict] = []

    # Sort by mtime desc — most recent first
    for f in sorted(p.rglob("*"), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
        if not f.is_file():
            continue
        rel = f.relative_to(p)
        name = rel.name.lower()
        is_deliverable = any(name.endswith(ext) for ext in DELIVERABLE_BINARY_EXTS)

        # Skip files inside excluded dirs UNLESS they're high-value binary deliverables
        if any(part in skip_dirs or part.startswith(".") for part in rel.parts[:-1]):
            if is_deliverable:
                try:
                    deliverables.append({
                        "path": str(rel),
                        "abs_path": str(f),
                        "size_bytes": f.stat().st_size,
                        "preview": "(binary deliverable — not previewed)",
                    })
                except Exception:
                    pass
            continue

        # Skip dotfiles at any depth
        if rel.name.startswith("."):
            continue

        try:
            size = f.stat().st_size
            preview = ""
            try:
                if is_deliverable:
                    preview = "(binary deliverable — not previewed)"
                elif size < 200_000:
                    # Read text preview, replacing undecodable bytes
                    preview = f.read_text(errors="replace")[:max_preview]
            except Exception:
                preview = "(binary)"

            out.append({
                "path": str(rel),
                "abs_path": str(f),
                "size_bytes": size,
                "preview": preview,
            })
            if len(out) >= max_files:
                break
        except Exception:
            continue

    # Deliverables (e.g. APK) come FIRST in the response — they're what the
    # user actually cares about; the source files come after.
    return deliverables + out


def last_claude_output(log: list[dict]) -> str:
    """Find the last meaningful text Claude produced.

    Useful when there's no file artifact (e.g. a research task that returned
    a markdown answer inline). Walks the log in reverse, returning the first:
      1. `result` event with text (Claude's final summary)
      2. `text` event with text (last assistant message)

    Cap is 8000 chars — enough for a 2-page response, won't blow up the JSON
    payload.
    """
    for entry in reversed(log):
        kind = entry.get("kind")
        if kind == "result" and entry.get("text"):
            return entry["text"][:8000]

    for entry in reversed(log):
        kind = entry.get("kind")
        if kind == "text" and entry.get("text"):
            return entry["text"][:8000]

    return ""
