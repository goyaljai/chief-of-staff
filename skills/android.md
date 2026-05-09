# Android / Gradle — domain skills

Apply when the task involves Android, Kotlin/Java apps, or Gradle.

## Build verification

- "Done" requires `./gradlew assembleDebug` (or `build`) to have run AND succeeded — the `BUILD SUCCESSFUL` line must be visible in the log.
- Just generating files is not done.
- Just opening in Android Studio mentally is not done.

## DSL consistency

- Pick one Gradle DSL and stick with it: either Groovy (`*.gradle`) OR Kotlin (`*.gradle.kts`). Don't mix.
- Never delete `.kts` files just because they're harder; either keep them and fix their contents, or fully replace with Groovy equivalents.

## Wrapper integrity

- `gradlew`, `gradlew.bat`, `gradle/wrapper/gradle-wrapper.properties`, AND `gradle/wrapper/gradle-wrapper.jar` must all be present.
- A hand-written `gradle-wrapper.properties` without the JAR will fail with "Could not find or load main class".

## local.properties

- `local.properties` is machine-specific; don't treat it as a project deliverable.
- If the SDK path is hardcoded, verify the path actually exists on this machine.
- For a portable project, document SDK setup instead of committing `local.properties`.

## Repository configuration

- Don't downgrade `RepositoriesMode.FAIL_ON_PROJECT_REPOS` to `PREFER_SETTINGS` to make a build pass — that masks misconfiguration.
- Fix repository declarations properly: `google()` and `mavenCentral()` in `settings.gradle.kts`, no project-level repositories blocks that conflict.

## Compose vs XML

- If the brief says XML views, no Compose dependencies should appear in `build.gradle`.
- If a template was used, double-check no Compose leftover is dragging Compose runtime in.

## Manifest correctness

- The launcher activity must be declared with the launcher intent filter.
- `applicationId`, `namespace`, manifest package name, and Kotlin package paths must all align.

## Min SDK respect

- If brief says minSdk = N, that exact value should be in `build.gradle` (`minSdk N`, not `minSdkVersion N` if using newer DSL).
