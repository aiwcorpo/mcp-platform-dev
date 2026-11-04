#!/bin/sh
# One-shot setup of the dev OpenBao (raft on the volume openbao-data, unsealed by openbao/unseal.sh; runs on every `make up`).
# Idempotent and safe on data that survived a restart: it creates what is missing and never overwrites Transit keys, user
# secrets (connections, authenticator ciphertext) or the Service Desk service account. Dev only.
# Layout used by the credential broker:
#   KV v2  secret/        connections: secret/data/mcp/connections/<sub>/<system>
#   KV v2  secret/data/mcp/oauth-clients/<system>: OAuth client of the broker at a provider (read only for the broker)
#   KV v2  secret/data/mcp/service-accounts/<system>: key of the platform's service account (credential type
#          service-account); today only servicedesk, field `value` = SERVICEDESK_API_KEY of .secrets/dev.env (the key
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

# `bao status` exits 0 only for an initialised and unsealed server; openbao-unseal gets it there after a (re)start.
ready=0
for _ in $(seq 120); do
  if bao status >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[ "$ready" = 1 ] || { echo "openbao-init: OpenBao is not unsealed after 120 s (is the openbao-unseal service running?)" >&2; exit 1; }

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
# secret_id expires after 30 days (BAO_SECRET_ID_TTL; was 7 days while a restart recreated it); the AppRole now lives in persistent storage, so a valid one
# survives restarts with its remaining TTL, and `make up` or `make openbao-init` recreates an expired one. No CIDR binding: the
# broker may run on the host, and the source address of host traffic differs between Docker setups.
bao write auth/approle/role/mcp-credential-broker \
  token_policies=mcp-credential-broker token_ttl=1h token_max_ttl=4h secret_id_ttl="${BAO_SECRET_ID_TTL:-720h}" >/dev/null
bao write auth/approle/role/mcp-credential-broker/role-id role_id="$BAO_BROKER_ROLE_ID" >/dev/null

lookup="$(printf '%s' "$BAO_BROKER_SECRET_ID" \
    | bao write -format=json auth/approle/role/mcp-credential-broker/secret-id/lookup secret_id=- 2>/dev/null || true)"
# BAO_RENEW_SECRET_ID=1 (make openbao-renew, make m5-switch) replaces a secret_id that is still valid, so its TTL starts again.
# Without it a valid one is left alone, except when less than half of its TTL remains: a restart no longer recreates it (storage is
# persistent), so every run of this script (make up) keeps it from running out unnoticed.
epoch() { date -u -d "$(printf '%s' "$1" | sed 's/\.[0-9]*Z$//; s/Z$//; s/T/ /')" +%s 2>/dev/null || true; }
renew="${BAO_RENEW_SECRET_ID:-0}"
if [ "$renew" != 1 ] && printf '%s' "$lookup" | grep -q '"secret_id_accessor"'; then
  created="$(epoch "$(printf '%s' "$lookup" | sed -n 's/.*"creation_time": *"\([^"]*\)".*/\1/p' | head -1)")"
  expires="$(epoch "$(printf '%s' "$lookup" | sed -n 's/.*"expiration_time": *"\([^"]*\)".*/\1/p' | head -1)")"
  now="$(date -u +%s)"
  if [ -n "$created" ] && [ -n "$expires" ] && [ $(( (expires - now) * 2 )) -lt $(( expires - created )) ]; then
    renew=1
    echo "openbao-init: less than half of the broker secret_id TTL is left"
  fi
fi
if [ "$renew" = 1 ] && printf '%s' "$lookup" | grep -q '"secret_id_accessor"'; then
  accessor="$(printf '%s' "$lookup" | sed -n 's/.*"secret_id_accessor": *"\([^"]*\)".*/\1/p' | head -1)"
  printf '%s' "$accessor" | bao write auth/approle/role/mcp-credential-broker/secret-id-accessor/destroy secret_id_accessor=- >/dev/null
  lookup=""
  echo "openbao-init: broker secret_id replaced"
fi
if printf '%s' "$lookup" | grep -q '"secret_id_accessor"'; then
  echo "openbao-init: broker secret_id already present"
else
  create_secret_id() {
    printf '%s' "$BAO_BROKER_SECRET_ID" | bao write auth/approle/role/mcp-credential-broker/custom-secret-id secret_id=- >/dev/null 2>&1
  }
  if ! create_secret_id; then
    # An expired secret_id stays registered (its lookup fails, so there is no accessor to destroy) until the tidy job removes it;
    # without this the same value cannot be created again ("SecretID is already registered") and the broker cannot log in.
    bao write -f auth/approle/tidy/secret-id >/dev/null 2>&1
    created=0
    for _ in $(seq 30); do
      sleep 1
      if create_secret_id; then created=1; break; fi
    done
    [ "$created" = 1 ] || { echo "openbao-init: cannot recreate the broker secret_id (expired entry not removed by tidy)" >&2; exit 1; }
    echo "openbao-init: expired broker secret_id replaced"
  fi
fi

expires="$(printf '%s' "$BAO_BROKER_SECRET_ID" \
  | bao write -field=expiration_time auth/approle/role/mcp-credential-broker/secret-id/lookup secret_id=- 2>/dev/null || true)"
echo "openbao-init: broker secret_id expires ${expires:-unknown}"

# Client of the GitLab mock (mocks/systems/gitlab.py); the same secret is in the mock's environment. Written when absent or
# different from .secrets/dev.env (configuration derived from the environment, no user state).
current="$(bao kv get -field=client_secret secret/mcp/oauth-clients/gitlab 2>/dev/null || true)"
[ "$current" = "$GITLAB_OAUTH_CLIENT_SECRET" ] \
  || printf '%s' "$GITLAB_OAUTH_CLIENT_SECRET" | bao kv put secret/mcp/oauth-clients/gitlab client_id=mcp-broker client_secret=- >/dev/null

# Token-exchange client of the broker (RFC 8693 towards Keycloak, audience opensearch); same rule.
current="$(bao kv get -field=client_secret secret/mcp/oauth-clients/opensearch 2>/dev/null || true)"
[ "$current" = "$KC_CLIENT_MCP_CREDENTIAL_BROKER_SECRET" ] \
  || printf '%s' "$KC_CLIENT_MCP_CREDENTIAL_BROKER_SECRET" | bao kv put secret/mcp/oauth-clients/opensearch client_id=mcp-credential-broker client_secret=- >/dev/null

# Service account of the platform at Service Desk (the mock accepts the key as Bearer). Written only when absent: the
# value may have been rotated by hand, and a restart of the stack must not turn it back.
bao kv get secret/mcp/service-accounts/servicedesk >/dev/null 2>&1 \
  || printf '%s' "$SERVICEDESK_API_KEY" | bao kv put secret/mcp/service-accounts/servicedesk value=- >/dev/null

echo "openbao-init: kv v2 at secret/, transit keys mcp-credential-broker and mcp-authenticators, approle mcp-credential-broker ready"
