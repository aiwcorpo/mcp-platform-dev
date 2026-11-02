#!/usr/bin/env bash
# Refuses (exit 4) any `make up*` while the stand-in of profile bulk exists: those targets first remove gateway, broker and balancers,
# and only then would compose.sh refuse the `up`. Used as a first prerequisite of the up targets, like scripts/openbao-guard.sh.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
project="$(grep -s '^COMPOSE_PROJECT_NAME=' "$root/.env" | cut -d= -f2- || true)"
if docker inspect "${project:-mcp-dev}-mcp-bulk-1" >/dev/null 2>&1; then
  echo "bulk-guard: REFUSED. The stand-in of profile bulk replaces mcp-confluence; finish the test first: make bulk-off CONFIRM=1" >&2
  echo "bulk-guard: (if bulk-off cannot finish: scripts/bulk.sh off --confirm again, or see docs/m6.md, 'Po przerwaniu')." >&2
  exit 4
fi
