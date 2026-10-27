#!/usr/bin/env bash
# Extracts the WireMock stubs (C4, C5) and audit event vectors (C8) of a pinned mcp-contracts ref into
# out/contracts/. Reads the git object store only: the working copy of mcp-contracts is never mounted.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
env_value() { grep "^$1=" "$root/.env" | cut -d= -f2-; }
contracts_dir="${MCP_CONTRACTS_DIR:-$(env_value MCP_CONTRACTS_DIR)}"
ref="${MCP_CONTRACTS_REF:-$(env_value MCP_CONTRACTS_REF)}"
[[ "$contracts_dir" = /* ]] || contracts_dir="$root/$contracts_dir"
out="$root/out/contracts"

commit="$(git -C "$contracts_dir" rev-parse --verify --quiet "$ref^{commit}")" \
  || { echo "contracts: ref $ref not found in $contracts_dir" >&2; exit 1; }

if [[ -f "$out/REF" && "$(cat "$out/REF")" == "$ref $commit" ]]; then
  exit 0
fi

# Only the contents below out/contracts change; containers mount that directory itself, so a re-extract
# never leaves them with a deleted mount source.
mkdir -p "$out"
rm -rf "$out/mocks" "$out/test-vectors"
git -C "$contracts_dir" archive "$commit" mocks/wiremock test-vectors/audit-events | tar -x -C "$out"
echo "$ref $commit" > "$out/REF"
echo "contracts: extracted $ref ($commit) into $out"
