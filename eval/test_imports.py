"""Cheap smoke test — every server module imports cleanly at startup.

Catches the kind of regression where supervisor_loop.run() references
dag_executor at runtime but `import dag_executor` was lost from the top of
the file (bug #9 from the V3.5 audit). Module-load-time errors in callers
that aren't exercised in the per-feature unit tests slip through otherwise.
"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: F401  -- loads .env

modules = [
    "persistence", "rag", "runners", "workflows.dag",
    "agents", "supervisor_loop", "main",
]

print("=" * 60)
print("IMPORT SMOKE TEST")
print("=" * 60)
failed: list = []
for m in modules:
    try:
        importlib.import_module(m)
        print(f"  ✓ {m}")
    except Exception as e:
        print(f"  ✗ {m}: {type(e).__name__}: {e}")
        failed.append((m, str(e)))

# Sanity: confirm symbols that supervisor_loop expects to use at runtime
import supervisor_loop  # noqa: F401
import workflows.dag as _dag  # noqa: F401
assert hasattr(supervisor_loop, "dag_executor"), \
    "supervisor_loop did not import dag_executor — runtime NameError will fire on DAG path"
assert callable(_dag.execute_dag)
assert callable(_dag.interrupt_all_for)
print("  ✓ supervisor_loop has dag_executor symbol bound")
print("  ✓ dag_executor exposes execute_dag + interrupt_all_for")

print()
if failed:
    print(f"FAILED: {len(failed)} module(s):")
    for m, e in failed:
        print(f"  {m}: {e}")
    sys.exit(1)
print("=" * 60)
print("IMPORTS CLEAN ✓")
print("=" * 60)
