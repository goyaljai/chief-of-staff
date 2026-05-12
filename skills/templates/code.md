# Code build template

Use this scaffold whenever the task is "build a thing that runs" — CLI tool, web app, library, script, mobile app, API server.

## Brief skeleton

- **Objective**: one sentence stating the artifact + its core behavior.
- **Deliverable file(s)**: name the exact file(s). For binaries (APK, JAR, .exe, .pdf) name the binary path, not the source. For source-only deliverables (e.g. `script.py`), name the source file.
- **What needs to be built**: list the concrete capabilities the artifact must have, in user-action terms ("`python3 calc.py "1+2"` prints `3`"). Avoid implementation prose.
- **Done / acceptance criteria** (numbered, verifiable):
  1. The deliverable file exists at the path stated.
  2. Running `<verification command>` succeeds with `<observable output>`.
  3. Edge case A is handled (specify it).
  4. (Optional) tests pass / lint clean.
- **Constraints**: stdlib-only? specific runtime version? no network? state them up-front.
- **Quality bar**: what counts as "good"? Naming, comments, error handling depth.

## Common gotchas (encoded from prior tasks)

- Don't scaffold a full project tree when one file does the job. Prefer single-file deliverables for one-off scripts.
- The verification command MUST be runnable in the workspace as-is. Shell-quote arguments correctly so the user can paste it.
- For Android: the executor needs `ANDROID_HOME` + the SDK on PATH. If env_audit reports those missing, the deliverable must shift to a SDK-less artifact (e.g. a Kotlin .kts file the user can run via `kotlinc -script`) or escalate.
- For Python: name `.py` deliverables explicitly. The reviewer rejects `requirements.txt` + scaffolding as the deliverable.
- For Node: don't include `node_modules/` or `package-lock.json` as deliverables. The reviewer treats them as scaffolding.

## Reviewer notes

- Mark the binary / single-source-file as the deliverable. Exclude `gradlew`, `build.gradle*`, `settings.gradle*`, `package-lock.json`, `node_modules/`, `__pycache__/`, `.gitignore`.
- Reject vacuous deliverables (TODO-only, empty file, "I'll do it later" stub).
