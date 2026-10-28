#!/usr/bin/env bash
# Dev only. The gateway takes the first manifest of a server without a registry row from the server itself
# (MCP_REGISTRY_BOOTSTRAP=true in docker-compose.yml). After a tool definition changed (new definitionHash) the stored
# manifest no longer matches: this drops the rows taken over that way and restarts the gateway, which fetches them again.
# Rows registered by CI (source 'ci') are never touched. The production path is the CI PUT with an approved manifest.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
compose="$root/scripts/compose.sh"
"$compose" exec -T postgres psql -U gateway -d gateway -At -c "delete from registry_servers where source = 'bootstrap'" \
  | sed 's/^/registry-refresh: dropped rows: /'
"$compose" --profile full --profile m2 up -d --no-deps --force-recreate --wait mcp-gateway >/dev/null
for _ in $(seq 60); do
  count="$("$compose" exec -T postgres psql -U gateway -d gateway -At -c "select count(*) from registry_servers")"
  [ "$count" -ge 5 ] && break
  sleep 2
done
"$compose" exec -T postgres psql -U gateway -d gateway -At -c \
  "select server_id || ' ' || source || ' tools=' || jsonb_array_length(manifest->'tools') from registry_servers order by 1"
