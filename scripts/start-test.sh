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

# A job that fails: up --wait fails, and init-failed.sh (called by the up targets of the Makefile) stops the service that depends on it.
c down -v >/dev/null 2>&1
sed -i.bak 's/"echo job-ran"/"exit 7"/' "$tmp/compose.yml" && rm -f "$tmp/compose.yml.bak"
cat > "$tmp/compose-wrapper.sh" <<WRAP
#!/bin/sh
exec docker compose -p $project -f $tmp/compose.yml "\$@"
WRAP
chmod +x "$tmp/compose-wrapper.sh"
if c up -d --wait >/dev/null 2>&1; then up_status=0; else up_status=1; fi
check "a failed job makes up --wait fail" "1" "$up_status"
INIT_FAILED_COMPOSE="$tmp/compose-wrapper.sh" INIT_FAILED_JOBS=init INIT_FAILED_PROFILES=0 "$root/scripts/init-failed.sh" >/dev/null 2>&1
check "init-failed.sh stops the service that depends on the failed job" "exited" "$(state)"

echo "start-test: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
