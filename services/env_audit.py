"""Quick environment audit run at task start (G8).

Why this exists
---------------
On 2026-05-11 the live RN-webview test had Claude grinding for 5+
minutes on gradle wrapper repair before realising Android SDK was
missing. The supervisor's escalation rule tells Claude to bail when an
env wall hits, but by then it had already wasted a chunk of the loop
budget. The smarter move: probe the env BEFORE scaffolding starts and
feed the answer into the brief, so the orchestrator can pivot the
deliverable shape ("no Android SDK → propose Expo Go QR" instead of
"silently grind on gradle and discover the wall mid-build").

Design
------
A single ``audit()`` call runs ~10 probes in parallel via thread pool,
each capped at 3s. Total wall-time well under 1s on a healthy box. The
result is a flat dict::

    {
      "python3": "3.12.4",
      "node": "22.4.0",
      "npm": "10.8.1",
      "yarn": None,
      "gradle": None,
      "android_home": None,
      "xcode": "available",
      "docker": None,
      "git": "2.45.2",
      "rust": None,
    }

Truthy = installed/available; None = missing or errored.

The orchestrator gets this as a markdown block in the brief prompt
("Environment audit") so it can warn Claude up-front about missing
toolchains and propose realistic deliverables.
"""
from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed


_PROBE_TIMEOUT_SECS = 3


def _run_version(args: list[str]) -> str | None:
    """Run a `--version`-style probe. Returns the first non-empty line
    of stdout (or stderr — many tools print to stderr) trimmed, or None
    on failure / non-zero exit / timeout."""
    try:
        r = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECS,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if r.returncode != 0 and not (r.stdout or r.stderr):
        return None
    out = (r.stdout or r.stderr or "").strip().splitlines()
    return out[0].strip() if out else None


def _check_env_var(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def _check_xcode() -> str | None:
    """xcrun -f xcodebuild returns a path if Xcode CLT is installed."""
    try:
        r = subprocess.run(
            ["xcrun", "-f", "xcodebuild"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECS,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if r.returncode == 0 and (r.stdout or "").strip():
        return "available"
    return None


# (key, callable). All run in parallel.
_PROBES: dict[str, callable] = {
    "python3": lambda: _run_version(["python3", "--version"]),
    "node": lambda: _run_version(["node", "--version"]),
    "npm": lambda: _run_version(["npm", "--version"]),
    "pnpm": lambda: _run_version(["pnpm", "--version"]),
    "yarn": lambda: _run_version(["yarn", "--version"]),
    "gradle": lambda: _run_version(["gradle", "--version"]),
    "android_home": lambda: _check_env_var("ANDROID_HOME") or _check_env_var("ANDROID_SDK_ROOT"),
    "xcode": _check_xcode,
    "docker": lambda: _run_version(["docker", "--version"]),
    "git": lambda: _run_version(["git", "--version"]),
    "rust": lambda: _run_version(["cargo", "--version"]),
    "go": lambda: _run_version(["go", "version"]),
}


def audit() -> dict[str, str | None]:
    """Run all probes in parallel. Returns a mapping of tool name to a
    human-readable version string (or None if missing/errored)."""
    out: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=len(_PROBES)) as pool:
        futures = {pool.submit(fn): name for name, fn in _PROBES.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                out[name] = fut.result(timeout=_PROBE_TIMEOUT_SECS + 1)
            except Exception:
                out[name] = None
    return out


def render_brief_block(audit_result: dict[str, str | None]) -> str:
    """Format the audit dict as a brief-prompt markdown block. Only
    surfaces what's actually present + what's notably absent for the
    common task families."""
    if not audit_result:
        return ""
    present = sorted([k for k, v in audit_result.items() if v])
    missing = sorted([k for k, v in audit_result.items() if not v])
    lines = ["## Environment audit (executor's machine)\n"]
    if present:
        lines.append("**Available:** " + ", ".join(
            f"`{k}={audit_result[k]}`" if isinstance(audit_result[k], str) and len(audit_result[k]) <= 40
            else f"`{k}`" for k in present
        ))
    if missing:
        lines.append("**Missing:** " + ", ".join(f"`{k}`" for k in missing))
    lines.append("")
    lines.append(
        "If the deliverable requires a missing toolchain, escalate via the "
        "ESCALATION/WHY/OPTIONS format with the exact install command — do "
        "NOT silently fall back to a weaker deliverable."
    )
    return "\n".join(lines)
