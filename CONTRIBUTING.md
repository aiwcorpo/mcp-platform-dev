# Contributing

Changes go through a pull request.

- Needs Docker with Compose v2, `make`, `bash` 3.2 or newer, `python3` 3.10 or newer, `openssl`, `git`. Linux and macOS
  are supported; on Windows use WSL2 and run Docker, `make` and the scripts inside it. Building component images needs a
  JDK 25 in `JAVA_HOME`.
- `make ci` (same as `ci/run.sh`) runs the lint, unit, compose and style checks; the steps that need Docker are skipped
  without a daemon unless `CI_REQUIRE_DOCKER=1`. Some targets restart or reset containers of the stack; read the target
  in `make help` before running it on a stack you need to keep.
- Scripts stay portable: no bash 4 features, no hard-coded Homebrew paths, `sed -i.bak` instead of GNU-only `sed -i`.
  Tools such as the JDK come from the environment (`JAVA_HOME`) with a clear error when missing.
- Never commit `.secrets/`, `certs/`, `out/`, `artifacts/`, logs or generated results. Do not put real credentials in
  mocks or fixtures.
- Behaviour of the platform follows `mcp-contracts`; do not change contracts here.
- Commits: English subject up to 72 characters in the imperative mood, a body that describes the change.
