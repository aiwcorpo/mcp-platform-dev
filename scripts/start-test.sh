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

echo "start-test: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
