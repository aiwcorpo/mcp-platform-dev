#!/usr/bin/env bash
# Plays the registry pipeline of each MCP server (C5, CC-133, CC-161): PUT /admin/v1/registry/servers/<serverId> with the manifest
# built from the commit of the server repository (mcp-app/target/mcp-manifest.json, an artifact of the build, never a manifest fetched
# from the running server) and the certificate of THAT server, CN=mcp-registry-ci-<serverId>.
#   make registry-register                       all five servers, manifests from ../mcp-server-<name>/mcp-app/target/mcp-manifest.json
#   make registry-register SERVERS="mcp-jira"    selected servers
#   MANIFEST_DIR=<dir>                           manifests as <dir>/<serverId>.json instead
# What the gateway does with it: READ_ONLY tools of the reviewed manifest are approved, new or changed UPDATE/DESTRUCTIVE tools wait
# in pending_approval for an administrator (portal), a manifest with environment dev is accepted only by a gateway in dev mode.
# Exit code 1 when any server is not registered (HTTP 200). Dev only difference: the dev stack may also bootstrap (the gateway
# takes the manifest from the server, see registry-refresh.sh); a row registered here has source ci and registry-refresh.sh leaves it alone.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
servers="${SERVERS:-mcp-jira mcp-confluence mcp-gitlab mcp-servicedesk mcp-opensearch}"
failed=0
for server in $servers; do
  if [ -n "${MANIFEST_DIR:-}" ]; then
    manifest="$MANIFEST_DIR/$server.json"
  else
    manifest="$root/../mcp-server-${server#mcp-}/mcp-app/target/mcp-manifest.json"
  fi
  if [ ! -f "$manifest" ]; then
    echo "registry-register: $server: no manifest at $manifest (build the server first)" >&2
    failed=1
    continue
  fi
  if reply="$(python3 "$root/scripts/mcpdev.py" registry-put "$server" "$manifest")"; then
    echo "registry-register: $server: $reply"
  else
    echo "registry-register: $server: FAILED $reply" >&2
    failed=1
  fi
done
exit "$failed"
