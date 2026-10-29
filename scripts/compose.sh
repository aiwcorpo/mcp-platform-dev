#!/usr/bin/env bash
# docker compose with ports (.env) and generated secrets (.secrets/dev.env) available for interpolation.
# While M4_ACTIVE=1 (environment, or .env after make m4-switch) the M4 layer is added: docker-compose.m4.yml and m4.env.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
files=()
envs=()
if [ "${M4_ACTIVE:-$(grep -s '^M4_ACTIVE=' "$root/.env" | cut -d= -f2-)}" = 1 ]; then
  files=(-f "$root/docker-compose.m4.yml")
  envs=(--env-file "$root/m4.env")
fi
exec docker compose --project-directory "$root" -f "$root/docker-compose.yml" ${files[@]+"${files[@]}"} \
  --env-file "$root/.env" --env-file "$root/.secrets/dev.env" ${envs[@]+"${envs[@]}"} "$@"
