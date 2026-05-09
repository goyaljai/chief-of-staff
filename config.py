import os
from pathlib import Path

DATABRICKS_TOKEN = os.environ.get("DATABRICKS_TOKEN", "")
DATABRICKS_BASE_URL = os.environ.get("DATABRICKS_BASE_URL", "")
DATABRICKS_MODEL = os.environ.get("DATABRICKS_MODEL", "databricks-gpt-5-4")

if not DATABRICKS_TOKEN or not DATABRICKS_BASE_URL:
    import sys
    sys.stderr.write(
        "ERROR: DATABRICKS_TOKEN and DATABRICKS_BASE_URL must be set as environment variables.\n"
        "  export DATABRICKS_TOKEN='...'\n"
        "  export DATABRICKS_BASE_URL='https://your-workspace.gcp.databricks.com/ai-gateway/mlflow/v1'\n"
    )
    sys.exit(1)

PROJECT_ROOT = Path(__file__).parent.resolve()
PROMPTS_DIR = PROJECT_ROOT / "prompts"
HOOK_SCRIPT = PROJECT_ROOT / "permission_hook.py"

WORKSPACE_ROOT = Path(os.environ.get(
    "SUPERVISOR_WORKSPACE_ROOT",
    os.path.expanduser("~/Desktop/supervisor-workspace"),
))

LOG_ROOT = Path(os.environ.get(
    "SUPERVISOR_LOG_ROOT",
    os.path.expanduser("~/Desktop/supervisor-workspace/_logs"),
))

MAX_CORRECTION_LOOPS = 3
ESCALATION_AUTO_RESOLVE_SECS = 30 * 60

ALLOWED_TOOLS = ["Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep", "LS"]

WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
LOG_ROOT.mkdir(parents=True, exist_ok=True)
