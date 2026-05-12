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
The probe set lives in ``services/env_probes.json`` — a data file, NOT
hard-coded Python — so adding a new toolchain (JAVA_HOME, NVM_DIR,
M2_HOME, the next thing tomorrow needs) is a config edit, not a code
change. Each probe has a ``type`` and a small bag of type-specific
fields:

  - ``version_cmd``  — run a `--version`-style subprocess
  - ``env_path``     — read one or more env vars; report only when the
                       referenced path actually exists on disk
  - ``xcode_clt``    — special-case `xcrun -f xcodebuild` lookup

Probes run in parallel via a thread pool with a 3 s per-probe ceiling
and a hard 5 s overall ceiling. Total wall-time on a healthy box is
well under 1 s.

The orchestrator gets the result as a markdown block in the brief
prompt ("Environment audit") so it can warn Claude up-front about
missing toolchains and propose realistic deliverables.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECS = 3
_PROBES_FILE = Path(__file__).resolve().parent / "env_probes.json"


# ────────────────────────────────────────────────────────────────────
# Probe primitives
# ────────────────────────────────────────────────────────────────────

def _run_version(args: list[str]) -> str | None:
    """Run a `--version`-style probe. Returns the first non-empty line
    of stdout (or stderr — many tools print to stderr) trimmed, or
    None on failure / non-zero exit / timeout."""
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


def _check_env_path(*var_names: str) -> str | None:
    """Read one or more env vars in priority order and return the first
    one whose value points to an existing directory. Returns None when
    none are set, or all are stale (set to a path that no longer
    exists on disk).

    A stale env-var path is treated as "missing" because reporting it
    as available misleads the orchestrator into proposing builds that
    the executor can't run (Bug 3, Phase 3 audit r2).
    """
    for name in var_names:
        v = os.environ.get(name, "").strip()
        if not v:
            continue
        if not os.path.isdir(v):
            log.debug("[env_audit] %s=%s but path does not exist; skipping", name, v)
            continue
        return v
    return None


def _check_xcode_clt() -> str | None:
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


# ────────────────────────────────────────────────────────────────────
# Probe-type dispatch
# ────────────────────────────────────────────────────────────────────

def _build_probe_callable(spec: dict) -> Callable[[], str | None] | None:
    """Translate one JSON probe spec into a zero-arg callable that
    returns either a human-readable detection string or None."""
    ptype = spec.get("type")
    if ptype == "version_cmd":
        args = list(spec.get("args") or [])
        if not args:
            return None
        return lambda: _run_version(args)
    if ptype == "env_path":
        var_names = list(spec.get("vars") or [])
        if not var_names:
            return None
        return lambda: _check_env_path(*var_names)
    if ptype == "xcode_clt":
        return _check_xcode_clt
    log.warning("[env_audit] unknown probe type %r in registry; ignoring", ptype)
    return None


def _load_probes() -> dict[str, Callable[[], str | None]]:
    """Load probes from env_probes.json. Returns an empty dict if the
    file is missing or malformed — env_audit is a quality-of-life
    layer, never break the supervisor on a misconfig."""
    if not _PROBES_FILE.is_file():
        log.warning("[env_audit] %s missing; no probes will run", _PROBES_FILE)
        return {}
    try:
        with open(_PROBES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("[env_audit] failed to parse %s: %s", _PROBES_FILE, e)
        return {}
    out: dict[str, Callable[[], str | None]] = {}
    for spec in data.get("probes") or []:
        name = (spec or {}).get("name")
        if not name or name in out:
            continue
        fn = _build_probe_callable(spec)
        if fn is None:
            continue
        out[name] = fn
    return out


# Cached at import time. Tests can monkeypatch _PROBES if they want.
_PROBES: dict[str, Callable[[], str | None]] = _load_probes()


def reload_probes() -> dict[str, Callable[[], str | None]]:
    """Re-read env_probes.json. Useful when the JSON has been edited
    and you want the change picked up without restarting the server."""
    global _PROBES
    _PROBES = _load_probes()
    return _PROBES


def audit() -> dict[str, str | None]:
    """Run all probes in parallel. Returns a mapping of probe name to
    a human-readable detection string (or None if missing/errored).

    Bug fix (Phase 3 audit): the original ``with ThreadPoolExecutor``
    block would block on ``__exit__`` waiting for any thread that
    survived its 4s ``fut.result(timeout=...)`` call. Use
    ``shutdown(wait=False, cancel_futures=True)`` so a probe that
    deadlocks doesn't wedge the supervisor.
    """
    if not _PROBES:
        return {}
    out: dict[str, str | None] = {}
    pool = ThreadPoolExecutor(max_workers=len(_PROBES))
    try:
        futures = {pool.submit(fn): name for name, fn in _PROBES.items()}
        for fut in as_completed(futures, timeout=_PROBE_TIMEOUT_SECS + 2):
            name = futures[fut]
            try:
                out[name] = fut.result(timeout=_PROBE_TIMEOUT_SECS + 1)
            except Exception:
                out[name] = None
        for name in _PROBES:
            out.setdefault(name, None)
    except FuturesTimeout:
        for name in _PROBES:
            out.setdefault(name, None)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out


def render_brief_block(audit_result: dict[str, str | None]) -> str:
    """Format the audit dict as a brief-prompt markdown block. Surfaces
    what's present + what's notably absent so the orchestrator can
    pivot the deliverable shape if the obvious path is blocked."""
    if not audit_result:
        return ""
    present = sorted([k for k, v in audit_result.items() if v])
    missing = sorted([k for k, v in audit_result.items() if not v])
    lines = ["## Environment audit (executor's machine)\n"]
    if present:
        lines.append("**Available:** " + ", ".join(
            f"`{k}={audit_result[k]}`"
            if isinstance(audit_result[k], str) and len(audit_result[k]) <= 40
            else f"`{k}`"
            for k in present
        ))
    if missing:
        lines.append("**Missing:** " + ", ".join(f"`{k}`" for k in missing))
    lines.append("")
    lines.append(
        "If the deliverable requires a missing toolchain, escalate via "
        "the ESCALATION/WHY/OPTIONS format with the exact install command "
        "— do NOT silently fall back to a weaker deliverable."
    )
    # P1 #2 — env-audit-trust instruction. Without this, Claude does
    # 4-5 redundant `java -version` / `ls SDK` / `find gradle-wrapper.jar`
    # tool calls per Android task even though we already probed. Each
    # redundant call costs 1 review_action ($0.017+) plus an executor
    # turn. Trusting the audit cuts those entirely.
    lines.append(
        "TRUST THIS AUDIT — do NOT re-probe the environment with your "
        "own `java -version` / `ls`-on-SDK / `find gradle-wrapper.jar` "
        "/ `which adb` / similar. The values above are authoritative as "
        "of task start. If you genuinely need fresher data (e.g. "
        "after running an installer), say so and proceed."
    )
    return "\n".join(lines)


# Backwards-compat helpers (tests may import these by name).
def _check_env_var(name: str) -> str | None:
    """Single-name compatibility shim around _check_env_path."""
    return _check_env_path(name)
