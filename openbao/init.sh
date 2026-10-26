#!/bin/sh
# One-shot setup of the dev OpenBao (in-memory, so it runs on every `up`). Idempotent.
# Layout used by the credential broker (ws-06):
#   KV v2  secret/        connections: secret/data/mcp/connections/<sub>/<system>
#   Transit transit/      key mcp-credential-broker (envelope encryption of stored secrets)
#   AppRole mcp-credential-broker with policy limited to the above.
set -eu
export BAO_ADDR="${BAO_ADDR:-http://openbao:8200}"
export BAO_TOKEN="$BAO_ROOT_TOKEN"

until bao status >/dev/null 2>&1; do sleep 1; done

bao secrets list -format=json | grep -q '"secret/"' || bao secrets enable -path=secret -version=2 kv
bao secrets list -format=json | grep -q '"transit/"' || bao secrets enable transit
bao read transit/keys/mcp-credential-broker >/dev/null 2>&1 \
  || bao write -f transit/keys/mcp-credential-broker type=aes256-gcm96

bao policy write mcp-credential-broker - <<'HCL'
path "secret/data/mcp/connections/*"     { capabilities = ["create", "read", "update", "delete"] }
path "secret/metadata/mcp/connections/*" { capabilities = ["read", "list", "delete"] }
path "transit/encrypt/mcp-credential-broker" { capabilities = ["update"] }
path "transit/decrypt/mcp-credential-broker" { capabilities = ["update"] }
HCL

bao auth list -format=json | grep -q '"approle/"' || bao auth enable approle
bao write auth/approle/role/mcp-credential-broker \
  token_policies=mcp-credential-broker token_ttl=1h token_max_ttl=4h secret_id_ttl=0 >/dev/null
bao write auth/approle/role/mcp-credential-broker/role-id role_id="$BAO_BROKER_ROLE_ID" >/dev/null
bao write -f auth/approle/role/mcp-credential-broker/custom-secret-id secret_id="$BAO_BROKER_SECRET_ID" >/dev/null 2>&1 || true

echo "openbao-init: kv v2 at secret/, transit key mcp-credential-broker, approle mcp-credential-broker ready"
