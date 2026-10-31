#!/usr/bin/env bash
# Runs openbao/init.sh against a throw-away dev OpenBao in an isolated network (the running stack is not touched) and
# checks what the broker's AppRole may do with the service-account path (CC-157). Prints PASS/FAIL lines; exit 1 on FAIL.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
image="$(grep -m1 'image: openbao/openbao' "$root/docker-compose.yml" | sed 's/.*image: //')"
net="m4-bao-test-$$"
server="$net-server"
token="test-root-$$"
secret="sd-test-$$"
exchange_secret="kc-test-$$"
failed=0

cleanup() { docker rm -f "$server" >/dev/null 2>&1 || true; docker network rm "$net" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker network create "$net" >/dev/null
docker run -d --name "$server" --network "$net" --network-alias openbao --cap-add IPC_LOCK \
  -e BAO_DEV_ROOT_TOKEN_ID="$token" "$image" server -dev -dev-listen-address=0.0.0.0:8200 >/dev/null

bao() { docker run --rm -i --network "$net" -e BAO_ADDR=http://openbao:8200 -e BAO_TOKEN="$1" --entrypoint bao "$image" "${@:2}"; }
check() { if [ "$2" = ok ]; then echo "PASS $1"; else echo "FAIL $1"; failed=1; fi; }

for _ in $(seq 30); do bao "$token" status >/dev/null 2>&1 && break; sleep 1; done
docker run --rm --network "$net" -v "$root/openbao/init.sh:/init.sh:ro" \
  -e BAO_ADDR=http://openbao:8200 -e BAO_ROOT_TOKEN="$token" -e BAO_BROKER_ROLE_ID=broker-role \
  -e BAO_BROKER_SECRET_ID=broker-secret-id -e GITLAB_OAUTH_CLIENT_SECRET=gitlab-test -e SERVICEDESK_API_KEY="$secret" -e KC_CLIENT_MCP_CREDENTIAL_BROKER_SECRET="$exchange_secret" \
  --entrypoint /bin/sh "$image" /init.sh >/dev/null
check "init.sh runs to the end" ok
docker run --rm --network "$net" -v "$root/openbao/init.sh:/init.sh:ro" \
  -e BAO_ADDR=http://openbao:8200 -e BAO_ROOT_TOKEN="$token" -e BAO_BROKER_ROLE_ID=broker-role \
  -e BAO_BROKER_SECRET_ID=broker-secret-id -e GITLAB_OAUTH_CLIENT_SECRET=gitlab-test -e SERVICEDESK_API_KEY="$secret" -e KC_CLIENT_MCP_CREDENTIAL_BROKER_SECRET="$exchange_secret" \
  --entrypoint /bin/sh "$image" /init.sh >/dev/null && check "init.sh is idempotent" ok || check "init.sh is idempotent" fail

ttl_of() { printf '%s' broker-secret-id | bao "$token" write -field=expiration_time auth/approle/role/mcp-credential-broker/secret-id/lookup secret_id=- 2>/dev/null; }
sleep 3
renew_out="$(mktemp)"
before_ttl="$(ttl_of)"
docker run --rm --network "$net" -v "$root/openbao/init.sh:/init.sh:ro" \
  -e BAO_ADDR=http://openbao:8200 -e BAO_ROOT_TOKEN="$token" -e BAO_BROKER_ROLE_ID=broker-role -e BAO_RENEW_SECRET_ID=1 \
  -e BAO_BROKER_SECRET_ID=broker-secret-id -e GITLAB_OAUTH_CLIENT_SECRET=gitlab-test -e SERVICEDESK_API_KEY="$secret" -e KC_CLIENT_MCP_CREDENTIAL_BROKER_SECRET="$exchange_secret" \
  --entrypoint /bin/sh "$image" /init.sh > "$renew_out"
grep -q 'secret_id replaced' "$renew_out" && check "BAO_RENEW_SECRET_ID=1 replaces a valid secret_id" ok || check "BAO_RENEW_SECRET_ID=1 replaces a valid secret_id" fail
after_ttl="$(ttl_of)"
rm -f "$renew_out"
[ -n "$before_ttl" ] && [ -n "$after_ttl" ] && [[ "$after_ttl" > "$before_ttl" ]] && check "the renewed secret_id expires later ($before_ttl -> $after_ttl)" ok || check "the renewed secret_id expires later ($before_ttl -> $after_ttl)" fail

broker="$(printf '%s' broker-secret-id | bao "" write -field=token auth/approle/login role_id=broker-role secret_id=- 2>/dev/null || true)"
[ -n "$broker" ] && check "broker AppRole logs in" ok || check "broker AppRole logs in" fail

[ "$(bao "$broker" kv get -field=value secret/mcp/service-accounts/servicedesk 2>/dev/null)" = "$secret" ] \
  && check "broker reads secret/mcp/service-accounts/servicedesk (value equals SERVICEDESK_API_KEY)" ok \
  || check "broker reads secret/mcp/service-accounts/servicedesk (value equals SERVICEDESK_API_KEY)" fail
[ "$(bao "$broker" kv get -field=client_secret secret/mcp/oauth-clients/opensearch 2>/dev/null)" = "$exchange_secret" ] \
  && check "broker reads the token-exchange client secret at secret/mcp/oauth-clients/opensearch" ok \
  || check "broker reads the token-exchange client secret at secret/mcp/oauth-clients/opensearch" fail
[ "$(bao "$broker" kv get -field=client_secret secret/mcp/oauth-clients/gitlab 2>/dev/null)" = "gitlab-test" ] \
  && check "broker still reads secret/mcp/oauth-clients/gitlab" ok || check "broker still reads secret/mcp/oauth-clients/gitlab" fail
if printf '%s' x | bao "$broker" kv put secret/mcp/service-accounts/servicedesk key=- >/dev/null 2>&1; then
  check "broker cannot write the service account" fail
else check "broker cannot write the service account" ok; fi
if bao "$broker" kv delete secret/mcp/service-accounts/servicedesk >/dev/null 2>&1; then
  check "broker cannot delete the service account" fail
else check "broker cannot delete the service account" ok; fi
if bao "$broker" kv list secret/mcp/service-accounts >/dev/null 2>&1; then
  check "broker cannot list the service accounts" fail
else check "broker cannot list the service accounts" ok; fi
exit "$failed"
