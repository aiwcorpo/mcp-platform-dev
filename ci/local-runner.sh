#!/usr/bin/env bash
# Runs the jobs of .gitlab-ci.yml locally in the runner container (ci/runner.Dockerfile), or ci/run.sh directly in the CI image.
#   ci/local-runner.sh                 all jobs of .gitlab-ci.yml
#   ci/local-runner.sh static          one job
#   CI_MODE=image ci/local-runner.sh proxy lb      ci/run.sh steps in the CI image, without gitlab-ci-local
# The repository is mounted at the same absolute path as on the host: steps proxy and lb give bind mounts to the host daemon,
# which resolves them on the host. The Docker socket is mounted into the container, so a job there can do anything the
# daemon can; run only code of this repository. Nothing is pushed or registered.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
if [ "${CI_MODE:-runner}" = image ]; then
  docker image inspect mcp-ci:local >/dev/null 2>&1 || docker build -q -f "$root/ci/Dockerfile" -t mcp-ci:local "$root" >/dev/null
  exec docker run --rm -v "$root:$root" -w "$root" -v /var/run/docker.sock:/var/run/docker.sock mcp-ci:local "$@"
fi
docker image inspect mcp-ci:local >/dev/null 2>&1 || docker build -q -f "$root/ci/Dockerfile" -t mcp-ci:local "$root" >/dev/null
docker image inspect mcp-ci-runner:local >/dev/null 2>&1 || docker build -q -f "$root/ci/runner.Dockerfile" -t mcp-ci-runner:local "$root" >/dev/null
exec docker run --rm -v "$root:$root" -w "$root" -v /var/run/docker.sock:/var/run/docker.sock mcp-ci-runner:local "$@"
