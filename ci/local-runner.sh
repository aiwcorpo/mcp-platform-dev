#!/usr/bin/env bash
# Runs the jobs of .gitlab-ci.yml locally in the runner container (ci/runner.Dockerfile), or ci/run.sh directly in the CI image.
#   ci/local-runner.sh                 all jobs of .gitlab-ci.yml
#   ci/local-runner.sh static          one job
#   CI_MODE=image ci/local-runner.sh proxy lb      ci/run.sh steps in the CI image, without gitlab-ci-local
# The container gets a throw-away copy of the repository (a clone plus the uncommitted files), never the working tree: .secrets,
# certs/ and out/ of the real stack stay out of reach, and what the run generates (secrets, CA) is removed with the copy. The copy is
# mounted at its own absolute path on the host: steps proxy and lb hand bind mounts to the host daemon, which resolves them on the
# host. The Docker socket is mounted too, so a job there can do anything the daemon can; run only code of this repository.
# Nothing is pushed or registered.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
git clone -q --no-hardlinks "$root" "$work/repo"
while IFS= read -r -d '' file; do
  [ -f "$root/$file" ] || continue
  mkdir -p "$work/repo/$(dirname "$file")"
  cp -p "$root/$file" "$work/repo/$file"
done < <(cd "$root" && git ls-files -z --cached --others --exclude-standard)

docker image inspect mcp-ci:local >/dev/null 2>&1 || docker build -q -f "$root/ci/Dockerfile" -t mcp-ci:local "$root" >/dev/null
image=mcp-ci:local
run_command=(ci/run.sh)
if [ "${CI_MODE:-runner}" != image ]; then
  run_command=()
  docker image inspect mcp-ci-runner:local >/dev/null 2>&1 || docker build -q -f "$root/ci/runner.Dockerfile" -t mcp-ci-runner:local "$root" >/dev/null
  image=mcp-ci-runner:local
fi
docker run --rm -e CI=true -v "$work:$work" -w "$work/repo" -v /var/run/docker.sock:/var/run/docker.sock "$image" ${run_command[@]+"${run_command[@]}"} "$@"
