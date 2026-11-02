#!/usr/bin/env bash
# Called by the up targets of the Makefile when `compose up --wait` failed. With `required: false` on the dependency (needed so that
# `compose start` works once the one-shot job container is gone) compose starts a dependent service even though its job failed. This
# stops those services, so that a failed init does not leave a running broker or MCP server behind. Exit 0 always; the caller fails.
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
# INIT_FAILED_COMPOSE, INIT_FAILED_JOBS and INIT_FAILED_PROFILES=0 let scripts/start-test.sh run this against its own project.
compose="${INIT_FAILED_COMPOSE:-$root/scripts/compose.sh}"
read -r -a jobs <<<"${INIT_FAILED_JOBS:-openbao-init server-state-init registry-data-init}"
profiles=(--profile full --profile m2 --profile ci)
[ "${INIT_FAILED_PROFILES:-1}" = 1 ] || profiles=()
failed=()
for job in "${jobs[@]}"; do
  state="$("$compose" "${profiles[@]}" ps -a --format '{{.State}} {{.ExitCode}}' "$job" 2>/dev/null | head -1)"
  case "$state" in exited\ 0|"") ;; exited*) failed+=("$job") ;; esac
done
[ "${#failed[@]}" -gt 0 ] || exit 0
dependents="$("$compose" "${profiles[@]}" config --format json | python3 -I -c '
import json, sys
failed = set(sys.argv[1:])
config = json.load(sys.stdin)["services"]
print(" ".join(sorted(n for n, svc in config.items() if failed & set(svc.get("depends_on") or {}))))' "${failed[@]}")"
echo "init-failed: ${failed[*]} did not finish successfully; stopping what depends on it: $dependents" >&2
# shellcheck disable=SC2086
[ -z "$dependents" ] || "$compose" "${profiles[@]}" stop $dependents >&2
exit 0
