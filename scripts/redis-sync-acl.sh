#!/usr/bin/env bash
# Applies the ACL users defined by redis/*.rules (portal-bff, broker) to the RUNNING Redis; redis/start.sh only
# runs when the container is created and reads the same files. Every secret goes through stdin or the
# environment of the container, never through argv.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
set -a
# shellcheck disable=SC1091
. "$root/.secrets/dev.env"
set +a
container="$("$root/scripts/compose.sh" ps -q redis)"
{
  printf 'ACL SETUSER portal-bff reset on >%s %s\n' "$REDIS_PORTAL_BFF_PASSWORD" "$(cat "$root/redis/portal-bff.rules")"
  printf 'ACL SETUSER broker reset on >%s %s\n' "$REDIS_BROKER_PASSWORD" "$(cat "$root/redis/broker.rules")"
} | docker exec -i "$container" sh -c 'REDISCLI_AUTH="$REDIS_ADMIN_PASSWORD" redis-cli --user admin'
