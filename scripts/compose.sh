#!/usr/bin/env bash
# docker compose with ports (.env) and generated secrets (.secrets/dev.env) available for interpolation.
# While M4_ACTIVE=1 (environment, or .env after make m4-switch) the M4 layer is added: docker-compose.m4.yml and m4.env.
# While M5_ACTIVE=1 (same way, after make m5-switch) the M5 layer comes on top of it: docker-compose.m5.yml and m5.env.
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
base=(docker compose --project-directory "$root" -f "$root/docker-compose.yml" ${files[@]+"${files[@]}"} \
  --env-file "$root/.env" --env-file "$root/.secrets/dev.env" ${envs[@]+"${envs[@]}"})

# Refuses an `up` that would replace a running in-memory (-dev) OpenBao and so delete its secrets (scripts/openbao-guard.sh).
for arg in "$@"; do
  if [ "$arg" = up ]; then "$root/scripts/openbao-guard.sh" "$@"; break; fi
done
exec "${base[@]}" "$@"
