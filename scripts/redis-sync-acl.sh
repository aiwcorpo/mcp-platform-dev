#!/usr/bin/env bash
# Adds the portal-bff ACL user to the RUNNING Redis (redis/start.sh only runs when the container is created).
# Idempotent; the password goes through the environment of redis-cli, not through argv.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
set -a
. "$root/.secrets/dev.env"
set +a
docker exec -e PORTAL_PW="$REDIS_PORTAL_BFF_PASSWORD" "$(docker compose -p mcp-dev ps -q redis)" sh -c '
  REDISCLI_AUTH="$REDIS_ADMIN_PASSWORD" redis-cli --user admin ACL SETUSER portal-bff reset on ">$PORTAL_PW" \
    "resetkeys" "~portal:*" "resetchannels" "-@all" "+@read" "+@write" "+@keyspace" "+@hash" "+@string" "+@scripting" \
    "+ping" "+auth" "+hello" "+client|setname" "+client|setinfo" "+multi" "+exec" "+discard"'
