#!/usr/bin/env bash
# Extracts the WireMock stubs (C4, C5) and audit event vectors (C8) of a pinned mcp-contracts ref into
# out/contracts/. Reads the git object store only: the working copy of mcp-contracts is never mounted.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=image-source.sh
. "$root/scripts/image-source.sh"
env_value() { grep "^$1=" "$root/.env" | cut -d= -f2-; }
contracts_dir="${MCP_CONTRACTS_DIR:-$(env_value MCP_CONTRACTS_DIR)}"
ref="${MCP_CONTRACTS_REF:-$(env_value MCP_CONTRACTS_REF)}"
[[ "$contracts_dir" = /* ]] || contracts_dir="$root/$contracts_dir"
out="$root/out/contracts"

if [ "$(image_source)" = ghcr ]; then
  # No sibling checkout: the same two directories from the published npm package (integrity checked against the registry metadata).
  version="$(ghcr_value MCP_CONTRACTS_NPM_VERSION)"
  if [[ -f "$out/REF" && "$(cat "$out/REF")" == "npm $version" ]]; then
    exit 0
  fi
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  curl -fsS "https://registry.npmjs.org/@aiwcorpo%2fmcp-contracts/$version" -o "$work/meta.json"
  tarball="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["dist"]["tarball"])' "$work/meta.json")"
  curl -fsS "$tarball" -o "$work/package.tgz"
  python3 -I - "$work/meta.json" "$work/package.tgz" <<'PY'
import base64, hashlib, json, sys
expected = json.load(open(sys.argv[1]))["dist"]["integrity"]
algorithm, _, digest = expected.partition("-")
actual = base64.b64encode(hashlib.new(algorithm, open(sys.argv[2], "rb").read()).digest()).decode()
if actual != digest:
    sys.exit("contracts: integrity mismatch of the npm tarball")
PY
  mkdir -p "$out" "$work/x"
  tar -xzf "$work/package.tgz" -C "$work/x" package/mocks/wiremock package/test-vectors/audit-events
  rm -rf "$out/mocks" "$out/test-vectors"
  mkdir -p "$out/mocks" "$out/test-vectors"
  mv "$work/x/package/mocks/wiremock" "$out/mocks/wiremock"
  mv "$work/x/package/test-vectors/audit-events" "$out/test-vectors/audit-events"
  echo "npm $version" > "$out/REF"
  echo "contracts: extracted @aiwcorpo/mcp-contracts $version from npm into $out"
  exit 0
fi

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
