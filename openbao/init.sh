#!/bin/sh
# One-shot setup of the dev OpenBao (in-memory, so it runs on every `make up`). Idempotent.
# Layout used by the credential broker (ws-06):
#   KV v2  secret/        connections: secret/data/mcp/connections/<sub>/<system>
#   KV v2  secret/data/mcp/oauth-clients/<system>: OAuth client of the broker at a provider (read only for the broker)
#   KV v2  secret/data/mcp/service-accounts/<system>: key of the platform's service account (credential type
#          service-account, CC-157); today only servicedesk, field `value` = SERVICEDESK_API_KEY of .secrets/dev.env (the key
#          the Service Desk mock accepts; the broker reads `value` for Bearer, `username`+`password` for Basic, optional
#          `expires_at`; the scheme comes from the broker's system configuration). Read only for the broker.
#   KV v2  secret/data/mcp/oauth-clients/<system>: client of the broker at the provider (gitlab) or its token-exchange client at
#          the IdP (opensearch: client_id mcp-credential-broker, client_secret of that Keycloak client). Read only for the broker.
#   Transit transit/      key mcp-credential-broker (encryption of stored secrets)
#   Transit transit/      key mcp-authenticators (TOTP seeds of the Authenticator, contracts 3.0.1 C4: Transit only, no
#                         envelope); ciphertext under secret/data/mcp/authenticators/<sha256(sub)>[/...]
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
# A separate key: seeds can be rotated or revoked without touching stored credentials.
bao read transit/keys/mcp-authenticators >/dev/null 2>&1 \
  || bao write -f transit/keys/mcp-authenticators type=aes256-gcm96

bao policy write mcp-credential-broker - <<'HCL'
path "secret/data/mcp/connections/*"     { capabilities = ["create", "read", "update", "delete"] }
path "secret/metadata/mcp/connections/*" { capabilities = ["read", "list", "delete"] }
path "secret/data/mcp/oauth-clients/*"      { capabilities = ["read"] }
path "secret/data/mcp/service-accounts/*"   { capabilities = ["read"] }
path "transit/encrypt/mcp-credential-broker" { capabilities = ["update"] }
path "transit/decrypt/mcp-credential-broker" { capabilities = ["update"] }
path "secret/data/mcp/authenticators/*"     { capabilities = ["create", "read", "update", "delete"] }
path "secret/metadata/mcp/authenticators/*" { capabilities = ["read", "list", "delete"] }
path "transit/encrypt/mcp-authenticators" { capabilities = ["update"] }
path "transit/decrypt/mcp-authenticators" { capabilities = ["update"] }
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

# Token-exchange client of the broker (RFC 8693 towards Keycloak, audience opensearch).
printf '%s' "$KC_CLIENT_MCP_CREDENTIAL_BROKER_SECRET" | bao kv put secret/mcp/oauth-clients/opensearch client_id=mcp-credential-broker client_secret=- >/dev/null

# Service account of the platform at Service Desk (the mock accepts the key as Bearer).
printf '%s' "$SERVICEDESK_API_KEY" | bao kv put secret/mcp/service-accounts/servicedesk value=- >/dev/null

echo "openbao-init: kv v2 at secret/, transit keys mcp-credential-broker and mcp-authenticators, approle mcp-credential-broker ready"
