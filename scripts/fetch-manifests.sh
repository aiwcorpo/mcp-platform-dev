#!/usr/bin/env bash
# Downloads mcp-manifest.json of each MCP server from its GitHub Release (MCP_RELEASE_TAG in ghcr.env) into out/manifests/<serverId>.json,
# the layout MANIFEST_DIR of scripts/registry-register.sh expects. The release asset is the manifest the CI of the server built from the
# tagged commit; public repositories, no login needed. Used when MCP_IMAGE_SOURCE=ghcr instead of ../mcp-server-<name>/mcp-app/target.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=image-source.sh
. "$root/scripts/image-source.sh"
tag="$(ghcr_value MCP_RELEASE_TAG)"
out="$root/out/manifests"
mkdir -p "$out"
for server in ${SERVERS:-mcp-jira mcp-confluence mcp-gitlab mcp-servicedesk mcp-opensearch}; do
  url="https://github.com/aiwcorpo/mcp-server-${server#mcp-}/releases/download/$tag/mcp-manifest.json"
  curl -fsSL "$url" -o "$out/$server.json.part"
  python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["serverId"] == sys.argv[2], d["serverId"]' "$out/$server.json.part" "$server" \
    || { rm -f "$out/$server.json.part"; echo "fetch-manifests: $server: the manifest at $url names another server" >&2; exit 1; }
  mv "$out/$server.json.part" "$out/$server.json"
  echo "fetch-manifests: $server $tag -> $out/$server.json"
done
