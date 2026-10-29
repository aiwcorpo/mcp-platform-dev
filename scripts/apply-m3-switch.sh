#!/usr/bin/env bash
# Applies the M3 switch to the RUNNING stack without down/clean/reset: broker, the five MCP servers (starter beta-4, profile
# devstack) and the gateway (profile devstack) are recreated from the images in .env and docker-compose.yml, in dependency order.
# State is kept: PostgreSQL, Redis (AOF volume), the registry rows and the Authenticator registrations are not touched.
# Afterwards: scripts/m3.py checks the devstack log line of the gateway and the servers (make m3).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
compose="$root/scripts/compose.sh"
c=("$compose" --profile full --profile m2)

"${c[@]}" up -d --no-deps --wait mcp-credential-broker
"${c[@]}" up -d --no-deps --wait server-state-init mcp-jira mcp-confluence mcp-gitlab mcp-servicedesk mcp-opensearch
"${c[@]}" up -d --no-deps --wait mcp-gateway

for service in mcp-gateway mcp-jira mcp-confluence mcp-gitlab mcp-servicedesk mcp-opensearch; do
  # Captured first: grep -q would close the pipe early and, under pipefail, turn a match into a failure.
  log="$("${c[@]}" logs "$service" 2>&1)"
  if grep -q 'event=devstack_profile' <<<"$log"; then
    echo "apply-m3-switch: $service runs with profile devstack"
  else
    echo "apply-m3-switch: $service did not log event=devstack_profile" >&2
    exit 1
  fi
done
echo "apply-m3-switch: done; now: make proxy-test, make m1, make m2, make m3 (m3 deletes the Authenticator of alice), mcp-e2e/run-m2.sh"
