# Environment audit probes (G8)

The supervisor probes the executor's machine for available toolchains
before generating the brief. The probe set is **data-driven** in
`services/env_probes.json`. Adding a new toolchain is a JSON edit, not
a code change.

## Probe types

| Type | Fields | Behavior |
|---|---|---|
| `version_cmd` | `name`, `args` (list[str]) | Runs the command with a 3 s timeout; reports the first non-empty output line |
| `env_path` | `name`, `vars` (list[str]) | Reads each env var in order; reports the first one whose value resolves to an existing directory |
| `xcode_clt` | `name` | Special-case `xcrun -f xcodebuild` lookup (macOS only) |

## Currently probed (18 entries)

```json
{
  "probes": [
    {"type": "version_cmd", "name": "python3",  "args": ["python3",  "--version"]},
    {"type": "version_cmd", "name": "node",     "args": ["node",     "--version"]},
    {"type": "version_cmd", "name": "npm",      "args": ["npm",      "--version"]},
    {"type": "version_cmd", "name": "pnpm",     "args": ["pnpm",     "--version"]},
    {"type": "version_cmd", "name": "yarn",     "args": ["yarn",     "--version"]},
    {"type": "version_cmd", "name": "gradle",   "args": ["gradle",   "--version"]},
    {"type": "version_cmd", "name": "docker",   "args": ["docker",   "--version"]},
    {"type": "version_cmd", "name": "git",      "args": ["git",      "--version"]},
    {"type": "version_cmd", "name": "rust",     "args": ["cargo",    "--version"]},
    {"type": "version_cmd", "name": "go",       "args": ["go",       "version"]},
    {"type": "env_path",  "name": "android_home", "vars": ["ANDROID_HOME", "ANDROID_SDK_ROOT"]},
    {"type": "env_path",  "name": "java_home",    "vars": ["JAVA_HOME"]},
    {"type": "env_path",  "name": "gradle_home",  "vars": ["GRADLE_HOME"]},
    {"type": "env_path",  "name": "nvm_dir",      "vars": ["NVM_DIR"]},
    {"type": "env_path",  "name": "cargo_home",   "vars": ["CARGO_HOME"]},
    {"type": "env_path",  "name": "go_path",      "vars": ["GOPATH"]},
    {"type": "env_path",  "name": "m2_home",      "vars": ["M2_HOME", "MAVEN_HOME"]},
    {"type": "xcode_clt", "name": "xcode"}
  ]
}
```

## Adding a new probe

Edit `services/env_probes.json`, restart the server (or call
`services.env_audit.reload_probes()` at runtime). The brief that ships
to Claude on the next task will include the new probe's result.

For a brand-new probe shape (e.g. "check if a specific port is bound",
"check available disk space"), add a new `_PROBE_TYPES` entry and
extend `_build_probe_callable()` in `services/env_audit.py`.

## Lessons captured (today's audit)

### Audit r2 — stale env-var path
`_check_env_var("ANDROID_HOME")` originally returned the env var's
value verbatim, so a stale `ANDROID_HOME=/Users/me/old_sdk` (set in
the user's `.zshrc` but pointing to a deleted dir) was reported as
"available". The orchestrator believed the SDK was installed; the
executor blew up at gradle invocation. Fix: every `env_path` probe now
verifies `os.path.isdir(value)` before reporting it. Renamed the helper
to `_check_env_path` to make the contract explicit.

### Audit r3 — generalized ANDROID_HOME case
The original probe set was hand-coded in Python. User pushed back:
"today it's ANDROID_HOME, tomorrow it's anything else." Right call.
Fix: hoisted the probe set into `services/env_probes.json` with three
probe types (version_cmd, env_path, xcode_clt). Adding JAVA_HOME,
GRADLE_HOME, NVM_DIR, CARGO_HOME, GOPATH, M2_HOME (which we did in the
same commit) was a JSON-only change.

### Thread leak under hung probe
`audit()` originally used `with ThreadPoolExecutor(...) as pool` which
would block on `__exit__` waiting for a probe whose thread survived
its 4 s collection timeout. Fix: explicit `pool.shutdown(wait=False,
cancel_futures=True)` and an outer `as_completed` timeout, with
unhealthy probes filled in as `None`.

## Safe defaults

- All `subprocess.run` calls have a 3 s ceiling.
- The pool's outer `as_completed` has a 5 s ceiling.
- The whole `audit()` is wrapped in try/except — a probe explosion is
  logged as `env_audit_failed` and the brief is built without an env
  block (degrades gracefully to "no env context").
