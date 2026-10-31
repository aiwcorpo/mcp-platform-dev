#!/usr/bin/env bash
# Pushes the component images of the stack (the tags in force: .env, m4.env, m5.env) to the local registry of D12
# (make m5-artifacts), as localhost:REGISTRY_PORT/<repository>:<tag>. Plain http on loopback; no credentials.
#   scripts/push-images.sh              all nine component images
#   scripts/push-images.sh mcp-gateway:m5-abc1234 ...   only these
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
port="$(grep '^REGISTRY_PORT=' "$root/m5.env" | cut -d= -f2-)"
if [ "$#" -gt 0 ]; then
  images=("$@")
else
  images=()
  while IFS= read -r line; do images+=("$line"); done < <(M4_ACTIVE=1 M5_ACTIVE=1 "$root/scripts/compose.sh" --profile full --profile m2 config --format json | python3 -c '
import json, sys
names = ("mcp-gateway", "mcp-credential-broker", "portal-bff", "portal-web", "mcp-jira", "mcp-confluence", "mcp-gitlab", "mcp-servicedesk", "mcp-opensearch")
services = json.load(sys.stdin)["services"]
print("\n".join(services[n]["image"] for n in names))')
fi
curl -sf "http://localhost:$port/v2/" >/dev/null || { echo "push-images: the registry does not answer on $port (make m5-artifacts)" >&2; exit 1; }
for image in "${images[@]}"; do
  target="localhost:$port/$image"
  docker tag "$image" "$target"
  docker push -q "$target" >/dev/null
  echo "push-images: $target"
done
