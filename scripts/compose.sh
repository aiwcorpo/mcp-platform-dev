#!/usr/bin/env bash
# docker compose with ports (.env) and generated secrets (.secrets/dev.env) available for interpolation.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
exec docker compose --project-directory "$root" -f "$root/docker-compose.yml" \
  --env-file "$root/.env" --env-file "$root/.secrets/dev.env" "$@"
