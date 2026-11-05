#!/usr/bin/env bash
# docker compose with ports (.env) and generated secrets (.secrets/dev.env) available for interpolation.
# Layers, in the order the env files are given (the later one wins): M4_ACTIVE=1 (docker-compose.m4.yml, m4.env), M5_ACTIVE=1 (docker-compose.m5.yml, m5.env),
# M6_ACTIVE=1 (m6.env: gateway and broker), M6B_ACTIVE=1 (m6b.env: contracts 6.2.0), M6C_ACTIVE=1 (m6c.env: contracts 6.3.2, scripts/apply-m6c-switch.sh).
# Each flag comes from the environment or, failing that, from .env. Once out/m6c.applied exists, a create without the M6C layer is refused (see below).
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
if [ "${M6C_ACTIVE:-$(grep -s '^M6C_ACTIVE=' "$root/.env" | cut -d= -f2-)}" = 1 ]; then
  [ "${M6B_ACTIVE:-$(grep -s '^M6B_ACTIVE=' "$root/.env" | cut -d= -f2-)}" = 1 ] || { echo "compose.sh: M6C_ACTIVE=1 needs the M6B layer (M6B_ACTIVE=1)" >&2; exit 1; }
  envs+=(--env-file "$root/m6c.env")
fi
# Last env file, so it wins over every layer above. Source local adds nothing: the invocation is the same as before the switch existed.
# shellcheck source=image-source.sh
. "$root/scripts/image-source.sh"
source_name="$(image_source)"
if [ "$source_name" = ghcr ]; then envs+=(--env-file "$root/ghcr.env"); fi
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

# Refuses to create containers (up, create, run) without the M6C layer once migration V10 of the gateway is in the database: an older gateway image would
# run on a schema it does not know. The marker out/m6c.applied is outside git and a variable M6C_ACTIVE=0 does not override it; without the marker the schema is
# read, but only when PostgreSQL already runs, so that a `make up` from scratch is never blocked. --dry-run only plans and is let through.
# shellcheck source=m6-layers.sh
. "$root/scripts/m6-layers.sh"
creates=0
for arg in "$@"; do
  case "$arg" in up|create|run) creates=1 ;; --dry-run) creates=0; break ;; esac
done
if [ "$creates" = 1 ] && [ "$(layer_flag M6C_ACTIVE)" != 1 ] && v10_applied --check-db; then
  echo "compose.sh: REFUSED. Migration V10 of the gateway is in the database ($(m6c_marker) or schema version >= 10) and M6C_ACTIVE is not 1:" >&2
  echo "compose.sh: an older gateway image would run on it. Set M6C_ACTIVE=1 in .env (m6c.env) and drop the variable from the environment." >&2
  exit 5
fi

# Refuses an `up` that would replace a running in-memory (-dev) OpenBao and so delete its secrets (scripts/openbao-guard.sh).
for arg in "$@"; do
  if [ "$arg" = up ]; then "$root/scripts/openbao-guard.sh" "$@"; break; fi
done
exec "${base[@]}" "$@"
