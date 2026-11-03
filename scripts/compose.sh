#!/usr/bin/env bash
# docker compose with ports (.env) and generated secrets (.secrets/dev.env) available for interpolation.
# While M4_ACTIVE=1 (environment, or .env after make m4-switch) the M4 layer is added: docker-compose.m4.yml and m4.env.
# While M5_ACTIVE=1 (same way, after make m5-switch) the M5 layer comes on top of it: docker-compose.m5.yml and m5.env.
# While M6B_ACTIVE=1 m6b.env follows it: the images of stage 3 (contracts 6.2.0).
# While M6_ACTIVE=1 (scripts/apply-m6-switch.sh) m6.env comes last: only the image tags of the gateway and the broker.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
files=()
envs=()
if [ "${M4_ACTIVE:-$(grep -s '^M4_ACTIVE=' "$root/.env" | cut -d= -f2-)}" = 1 ]; then
  files=(-f "$root/docker-compose.m4.yml")
  envs=(--env-file "$root/m4.env")
fi
if [ "${M5_ACTIVE:-$(grep -s '^M5_ACTIVE=' "$root/.env" | cut -d= -f2-)}" = 1 ]; then
  [ "${#files[@]}" -gt 0 ] || { echo "compose.sh: M5_ACTIVE=1 needs the M4 layer (M4_ACTIVE=1)" >&2; exit 1; }
  files+=(-f "$root/docker-compose.m5.yml")
  envs+=(--env-file "$root/m5.env")
fi
if [ "${M6_ACTIVE:-$(grep -s '^M6_ACTIVE=' "$root/.env" | cut -d= -f2-)}" = 1 ]; then
  [ "${#files[@]}" -gt 1 ] || { echo "compose.sh: M6_ACTIVE=1 needs the M5 layer (M5_ACTIVE=1)" >&2; exit 1; }
  envs+=(--env-file "$root/m6.env")
fi
if [ "${M6B_ACTIVE:-$(grep -s '^M6B_ACTIVE=' "$root/.env" | cut -d= -f2-)}" = 1 ]; then
  [ "${M6_ACTIVE:-$(grep -s '^M6_ACTIVE=' "$root/.env" | cut -d= -f2-)}" = 1 ] || { echo "compose.sh: M6B_ACTIVE=1 needs the M6 layer (M6_ACTIVE=1)" >&2; exit 1; }
  envs+=(--env-file "$root/m6b.env")
fi
base=(docker compose --project-directory "$root" -f "$root/docker-compose.yml" ${files[@]+"${files[@]}"} \
  --env-file "$root/.env" --env-file "$root/.secrets/dev.env" ${envs[@]+"${envs[@]}"})

# Refuses an `up` while the stand-in of profile bulk exists: the real mcp-confluence would start next to it under the same address.
# scripts/bulk.sh sets BULK_SWITCH=1 for its own calls.
project_name="$(grep -s '^COMPOSE_PROJECT_NAME=' "$root/.env" | cut -d= -f2- || true)"
for arg in "$@"; do
  if [ "$arg" = up ] && [ "${ADAPTER_SWITCH:-0}" != 1 ] && docker inspect "${project_name:-mcp-dev}-mcp-jira-adapter-1" >/dev/null 2>&1; then
    echo "compose.sh: REFUSED. The adapter of profile adapter replaces mcp-jira; an up would start the native one next to it." >&2
    echo "compose.sh: finish the demo first: scripts/adapter.sh off --confirm." >&2
    exit 4
  fi
  if [ "$arg" = up ] && [ "${BULK_SWITCH:-0}" != 1 ] && docker inspect "${project_name:-mcp-dev}-mcp-bulk-1" >/dev/null 2>&1; then
    echo "compose.sh: REFUSED. The stand-in of profile bulk replaces mcp-confluence; an up would start the real one next to it." >&2
    echo "compose.sh: finish the test first: scripts/bulk.sh off --confirm (make bulk-off CONFIRM=1)." >&2
    exit 4
  fi
done

# Refuses an `up` that would replace a running in-memory (-dev) OpenBao and so delete its secrets (scripts/openbao-guard.sh).
for arg in "$@"; do
  if [ "$arg" = up ]; then "$root/scripts/openbao-guard.sh" "$@"; break; fi
done
exec "${base[@]}" "$@"
