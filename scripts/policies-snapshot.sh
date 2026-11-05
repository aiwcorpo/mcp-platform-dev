#!/usr/bin/env bash
# Copies a signed policy bundle (both channels) and the dev public keys into policies-dev/, which the stack reads when
# MCP_IMAGE_SOURCE=ghcr (ghcr.env), so that a machine without JDK 25 and without ../mcp-policies still starts with signed policies.
# Only public keys are copied; the private keys stay in mcp-policies/keys/dev (ignored by git there) and never leave that machine.
# Build the bundles first, without publishing (they must not land in out/repository*, which the running stack reads):
#   (cd ../mcp-policies && . .ci/dev.env && .ci/policy-tool.sh build --channel main --git-sha "$(git rev-parse HEAD)" \
#      --created-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --out <dir>)     # and the same with --channel overrides
#   scripts/policies-snapshot.sh <dir>
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
from="${1:?usage: policies-snapshot.sh <dir with main/ and overrides/>}"
keys="${POLICY_KEYS_SOURCE:-$root/../mcp-policies/keys/dev}"
for channel in main overrides; do
  [ -s "$from/$channel/latest.json" ] || { echo "policies-snapshot: no $from/$channel/latest.json" >&2; exit 1; }
  key="$keys/dev-policies-$channel.public.pem"
  [ -s "$key" ] || { echo "policies-snapshot: missing $key" >&2; exit 1; }
  archive="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$from/$channel/latest.json")"
  # Same check the receivers do: Ed25519 over the archive bytes.
  openssl pkeyutl -verify -rawin -pubin -inkey "$key" -in "$from/$channel/$archive" -sigfile "$from/$channel/$archive.sig" >/dev/null \
    || { echo "policies-snapshot: $channel archive does not verify with $key" >&2; exit 1; }
  rm -rf "$root/policies-dev/repository/$channel"
  mkdir -p "$root/policies-dev/repository/$channel" "$root/policies-dev/keys"
  cp "$from/$channel/latest.json" "$from/$channel/$archive" "$from/$channel/$archive.sig" "$root/policies-dev/repository/$channel/"
  cp "$key" "$root/policies-dev/keys/"
  echo "policies-snapshot: $channel $archive"
done
