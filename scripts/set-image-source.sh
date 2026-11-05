#!/usr/bin/env bash
# Writes MCP_IMAGE_SOURCE into .env (make use-ghcr, make use-local). Changes nothing else and starts nothing.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
value="${1:?usage: set-image-source.sh local|ghcr}"
case "$value" in local|ghcr) ;; *) echo "set-image-source: local or ghcr" >&2; exit 2 ;; esac
if grep -q '^MCP_IMAGE_SOURCE=' "$root/.env"; then
  sed -i.bak "s/^MCP_IMAGE_SOURCE=.*/MCP_IMAGE_SOURCE=$value/" "$root/.env" && rm -f "$root/.env.bak"
else
  printf '\nMCP_IMAGE_SOURCE=%s\n' "$value" >> "$root/.env"
fi
echo "set-image-source: MCP_IMAGE_SOURCE=$value in .env; the next make up-* uses it (containers already running are not touched)"
