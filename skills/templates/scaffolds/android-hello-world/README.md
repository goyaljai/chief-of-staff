# Android Hello World scaffold (P3 #11)

A pre-built starter the supervisor seeds into the workspace at task start
when the task family is "android" and the env_audit confirms gradle +
ANDROID_HOME are available. Cuts ~7 boilerplate write_file calls per
Android task — biggest single volume lever for that family.

## What's NOT in this scaffold (intentional)

This skeleton holds only what's absolutely required to compile a debug
APK. **Not included:**
- `build.gradle` files (root + app) — written per-task because version
  numbers / SDK targets / package name vary
- `settings.gradle` — same reason
- `gradle/wrapper/gradle-wrapper.properties` — same reason
- `gradle/wrapper/gradle-wrapper.jar` — usually copied from a known-
  good local Android project at task time

The orchestrator brief tells Claude:

  > A scaffold exists at `skills/templates/scaffolds/android-hello-world/`.
  > Copy it into the workspace, write the per-task gradle files, then
  > modify the included MainActivity.kt / activity_main.xml /
  > strings.xml as needed. DO NOT re-write the Manifest unless you
  > need a permission the scaffold doesn't have.

## Future scaffolds

Drop a sibling directory + add to `_pick_scaffold_dir()` in
`agents/orchestrator.py` (pending Phase 5). Scaffold candidates:
- `react-native-webview-app/`
- `node-express-server/`
- `python-cli-tool/`
