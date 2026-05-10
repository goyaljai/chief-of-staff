"""DAG validation — runs before invoke to reject malformed step lists.

Three validations:

  1. Step ID safety. IDs are used as filename suffixes (`hook-{id}.log`)
     and could otherwise escape the workspace via `Path(...) / id`.
     We require [A-Za-z0-9_-]{1,64} — no slashes, no dots, no spaces.
     LLM-hallucinated IDs containing "/", "..", spaces are rejected here.
  2. Reference integrity. Every entry in `depends_on` must point at a
     real step ID — otherwise the DAG would deadlock waiting on a
     non-existent prerequisite.
  3. Cycle detection via Kahn's algorithm topological sort. A cycle
     would deadlock the dispatcher's "all deps done?" check forever.
"""
import re


_SAFE_STEP_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _validate_dag(steps: list[dict]) -> None:
    """Raise ValueError on duplicate ids, unsafe ids, dangling deps, or
    cycles. Returns None on success."""
    ids = {s["id"] for s in steps}
    if len(ids) != len(steps):
        raise ValueError("dag has duplicate step ids")
    for s in steps:
        sid = s.get("id", "")
        if not isinstance(sid, str) or not _SAFE_STEP_ID.match(sid):
            raise ValueError(
                f"step id {sid!r} must match [A-Za-z0-9_-]{{1,64}}"
            )
        for dep in s.get("depends_on", []):
            if dep not in ids:
                raise ValueError(f"step {sid!r} depends on unknown step {dep!r}")

    # Topological sort via Kahn's algorithm.
    indeg = {s["id"]: 0 for s in steps}
    for s in steps:
        for d in s.get("depends_on", []):
            indeg[s["id"]] += 1
    children: dict[str, list[str]] = {sid: [] for sid in ids}
    for s in steps:
        for d in s.get("depends_on", []):
            children[d].append(s["id"])
    queue = [sid for sid, n in indeg.items() if n == 0]
    seen = 0
    while queue:
        sid = queue.pop()
        seen += 1
        for child in children[sid]:
            indeg[child] -= 1
            if indeg[child] == 0:
                queue.append(child)
    if seen != len(steps):
        raise ValueError("dag has a cycle")
