#!/bin/sh
# One-shot setup of the dev OpenBao (in-memory, so it runs on every `make up`). Idempotent.
# Layout used by the credential broker (ws-06):
#   KV v2  secret/        connections: secret/data/mcp/connections/<sub>/<system>
#   KV v2  secret/data/mcp/oauth-clients/<system>: OAuth client of the broker at a provider (read only for the broker)
#   Transit transit/      key mcp-credential-broker (envelope encryption of stored secrets)
#   AppRole mcp-credential-broker with policy limited to the above
#   (audit device to stdout is declared in openbao/audit.hcl; OpenBao 2.x refuses it via API)
# Secrets go through stdin (`key=-`), never argv.
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
path "secret/data/mcp/oauth-clients/*"   { capabilities = ["read"] }
path "transit/encrypt/mcp-credential-broker" { capabilities = ["update"] }
path "transit/decrypt/mcp-credential-broker" { capabilities = ["update"] }
HCL

bao auth list -format=json | grep -q '"approle/"' || bao auth enable approle
# secret_id expires after 7 days; `make up` re-runs this script and recreates it. No CIDR binding: the
# broker may run on the host, and the source address of host traffic differs between Docker setups.
bao write auth/approle/role/mcp-credential-broker \
  token_policies=mcp-credential-broker token_ttl=1h token_max_ttl=4h secret_id_ttl=168h >/dev/null
bao write auth/approle/role/mcp-credential-broker/role-id role_id="$BAO_BROKER_ROLE_ID" >/dev/null

if printf '%s' "$BAO_BROKER_SECRET_ID" \
    | bao write -format=json auth/approle/role/mcp-credential-broker/secret-id/lookup secret_id=- 2>/dev/null \
    | grep -q '"secret_id_accessor"'; then
  echo "openbao-init: broker secret_id already present"
else
  printf '%s' "$BAO_BROKER_SECRET_ID" \
    | bao write auth/approle/role/mcp-credential-broker/custom-secret-id secret_id=- >/dev/null
fi

# Client of the GitLab mock (mocks/systems/gitlab.py); the same secret is in the mock's environment.
printf '%s' "$GITLAB_OAUTH_CLIENT_SECRET" | bao kv put secret/mcp/oauth-clients/gitlab client_id=mcp-broker client_secret=- >/dev/null

echo "openbao-init: kv v2 at secret/, transit key mcp-credential-broker, approle mcp-credential-broker ready"
