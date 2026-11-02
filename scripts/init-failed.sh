#!/usr/bin/env bash
# Called by the up targets of the Makefile when `compose up --wait` failed:
#   scripts/init-failed.sh SINCE [compose profile arguments of the target...]      SINCE: UTC time taken just before the `up`, 2026-11-02T18:50:12Z
# With `required: false` on the dependency (needed so that `compose start` works once the one-shot job container is gone) compose starts a
# dependent service even though its job failed. This stops those services, so that a failed init leaves no running broker or MCP server behind.
# It acts only on what this `up` did: a job counts when it FINISHED with an error at or after SINCE (an old `exited N` container proves nothing
# about this run), only jobs of the profiles of the target count, and only services STARTED at or after SINCE are stopped; whatever ran before
# stays untouched. Exit 0 always; the caller fails.
# INIT_FAILED_COMPOSE and INIT_FAILED_JOBS let scripts/start-test.sh run this against its own project.
set -uo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
since="${1:?usage: init-failed.sh SINCE [profile arguments...]}"
shift
profiles=("$@")
compose="${INIT_FAILED_COMPOSE:-$root/scripts/compose.sh}"
read -r -a jobs <<<"${INIT_FAILED_JOBS:-openbao-init server-state-init registry-data-init}"

# Docker prints nanoseconds ("...:12.123456789Z"); cut to whole seconds so that a plain string comparison is right.
seconds() { local t="${1%%.*}"; echo "${t%Z}Z"; }
not_before() { [[ ! "$(seconds "$1")" < "$since" ]]; }
compose_cmd() { "$compose" ${profiles[@]+"${profiles[@]}"} "$@"; }

failed=()
for job in "${jobs[@]}"; do
  id="$(compose_cmd ps -a -q "$job" 2>/dev/null | head -1)"
  [ -n "$id" ] || continue
  read -r status code finished < <(docker inspect -f '{{.State.Status}} {{.State.ExitCode}} {{.State.FinishedAt}}' "$id")
  if [ "$status" = exited ] && [ "$code" != 0 ] && not_before "$finished"; then failed+=("$job"); fi
done
[ "${#failed[@]}" -gt 0 ] || exit 0

dependents="$(compose_cmd config --format json | python3 -I -c '
import json, sys
failed = set(sys.argv[1:])
config = json.load(sys.stdin)["services"]
print(" ".join(sorted(n for n, svc in config.items() if failed & set(svc.get("depends_on") or {}))))' "${failed[@]}")" \
  || { echo "init-failed: cannot read the configuration; services that depend on ${failed[*]} were NOT stopped" >&2; exit 0; }

to_stop=()
for service in $dependents; do
  id="$(compose_cmd ps -q "$service" 2>/dev/null | head -1)"
  [ -n "$id" ] || continue
  read -r status started < <(docker inspect -f '{{.State.Status}} {{.State.StartedAt}}' "$id")
  if [ "$status" = running ] && not_before "$started"; then to_stop+=("$service"); fi
done
echo "init-failed: ${failed[*]} failed in this run; stopping what started after it: ${to_stop[*]:-nothing}" >&2
[ "${#to_stop[@]}" -eq 0 ] || compose_cmd stop "${to_stop[@]}" >&2 || echo "init-failed: stop failed; check docker ps" >&2
exit 0
