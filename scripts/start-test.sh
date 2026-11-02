#!/usr/bin/env bash
# `docker compose start` after the one-shot job container is gone, on a throw-away project of its own (no ports, no volumes,
# nothing of the stack is touched): the service that depends on the job must start, and start must not recreate the job.
# Usage: scripts/start-test.sh
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
project=mcp-dev-starttest
image="$(sed -n 's/^    image: \(python:[^ ]*\)$/\1/p' "$root/docker-compose.yml" | head -1)"
tmp="$(mktemp -d)"
pass=0
fail=0
c() { docker compose -p "$project" -f "$tmp/compose.yml" "$@"; }
trap 'c down -v >/dev/null 2>&1; rm -rf "$tmp"' EXIT

check() { # name expected actual
  if [[ "$2" == "$3" ]]; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL $1: expected '$2', got '$3'"; fi
}
runs() { c logs init 2>/dev/null | grep -c job-ran; }
state() { c ps -a --format '{{.State}}' app 2>/dev/null; }

cat > "$tmp/compose.yml" <<YML
services:
  init:
    image: $image
    command: ["sh", "-c", "echo job-ran"]
    restart: "no"
  app:
    image: $image
    command: ["sleep", "600"]
    depends_on:
      init: {condition: service_completed_successfully, required: false}
YML

c up -d >/dev/null 2>&1
check "up runs the job and starts the service" "1 running" "$(runs) $(state)"
c stop >/dev/null 2>&1
c rm -f init >/dev/null 2>&1
c start >/dev/null 2>&1
check "start without the job container starts the service" "running" "$(state)"
check "start does not recreate the job" "0" "$(c ps -a -q init | wc -l | tr -d ' ')"
c stop >/dev/null 2>&1
c start >/dev/null 2>&1
check "second stop and start keeps the service up" "running" "$(state)"

# A job that fails. init-failed.sh gets the time taken before the up and acts only on what that up did.
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
sed -i.bak 's/"echo job-ran"/"exit 7"/' "$tmp/compose.yml" && rm -f "$tmp/compose.yml.bak"
cat > "$tmp/compose-wrapper.sh" <<WRAP
#!/bin/sh
exec docker compose -p $project -f $tmp/compose.yml "\$@"
WRAP
chmod +x "$tmp/compose-wrapper.sh"
init_failed() { INIT_FAILED_COMPOSE="$tmp/compose-wrapper.sh" INIT_FAILED_JOBS=init "$root/scripts/init-failed.sh" "$1" >/dev/null 2>&1; }
app_container="$project-app-1"

c down -v >/dev/null 2>&1
since="$(now)"
sleep 1
if c up -d --wait >/dev/null 2>&1; then up_status=0; else up_status=1; fi
check "a failed job makes up --wait fail" "1" "$up_status"
init_failed "$since"
check "a failed job of this up: the service that started after it is stopped" "exited" "$(state)"

# An old failed job container and a failure of something else: the job did not fail in this run, nothing is stopped.
docker start "$app_container" >/dev/null
sleep 1
since="$(now)"
init_failed "$since"
check "an old exited-7 job container does not stop a running service" "running" "$(state)"

# The job fails again on a healthy stack (a repeated run): the service that ran before the up is left alone.
docker start "$project-init-1" >/dev/null 2>&1
docker wait "$project-init-1" >/dev/null 2>&1
init_failed "$since"
check "a transient failure of a repeated job leaves the service that ran before untouched" "running" "$(state)"

echo "start-test: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
