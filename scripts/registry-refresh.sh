#!/usr/bin/env bash
# Dev only. The gateway takes the first manifest of a server without a registry row from the server itself
# (MCP_REGISTRY_BOOTSTRAP=true in docker-compose.yml). After a tool definition changed (new definitionHash) the stored
# manifest no longer matches: this drops the rows taken over that way and restarts the gateway, which fetches them again.
# Rows registered by CI (source 'ci') are never touched. The production path is the CI PUT with an approved manifest.
# Exit code 1 when the manifests of all MCP servers of docker-compose.yml are not back after two minutes.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
compose="$root/scripts/compose.sh"
psql_gateway() { "$compose" exec -T postgres psql -U gateway -d gateway -At -c "$1"; }

# MCP servers are the services that describe themselves to the registry (MCP_INTERNAL_URL).
expected="$("$compose" --profile full --profile m2 config --format json | python3 -c '
import json, sys
services = json.load(sys.stdin)["services"]
print(sum(1 for s in services.values() if "MCP_INTERNAL_URL" in (s.get("environment") or {})))')"

psql_gateway "delete from registry_servers where source = 'bootstrap'" | sed 's/^/registry-refresh: dropped rows: /'
"$compose" --profile full --profile m2 up -d --no-deps --force-recreate --wait mcp-gateway >/dev/null
count=0
for _ in $(seq 60); do
  count="$(psql_gateway "select count(*) from registry_servers" || echo 0)"
  [ "${count:-0}" -ge "$expected" ] && break
  sleep 2
done
psql_gateway "select server_id || ' ' || source || ' tools=' || jsonb_array_length(manifest->'tools') from registry_servers order by 1"
if [ "${count:-0}" -lt "$expected" ]; then
  echo "registry-refresh: only $count of $expected manifests are back (gateway log: docker logs mcp-dev-mcp-gateway-1)" >&2
  exit 1
fi
