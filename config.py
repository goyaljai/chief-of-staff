import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.resolve()
_env_file = _PROJECT_ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))

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

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USER_IDS = {
    x.strip() for x in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip()
}
SUPERVISOR_API_BASE_URL = os.environ.get("SUPERVISOR_API_BASE_URL", "http://localhost:8000")

PROJECT_ROOT = Path(__file__).parent.resolve()
PROMPTS_DIR = PROJECT_ROOT / "prompts"
HOOK_SCRIPT = PROJECT_ROOT / "permission_hook.py"
STATIC_DIR = PROJECT_ROOT / "static"
DB_PATH = Path(os.environ.get("CHIEF_DB_PATH", str(PROJECT_ROOT / "data" / "chief.db")))
CHROMA_PATH = Path(os.environ.get("CHIEF_CHROMA_PATH", str(PROJECT_ROOT / "data" / "chroma")))
WORKSPACE_TTL_DAYS = int(os.environ.get("WORKSPACE_TTL_DAYS", "30"))

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
