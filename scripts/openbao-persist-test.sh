#!/usr/bin/env bash
# Persistence of the dev OpenBao on a throw-away instance (own network, volumes and containers; the stack is not touched):
# server with openbao/server.hcl on a named volume, openbao/unseal.sh as the sidecar, openbao/init.sh as in the stack. Writes a
# user secret, a Transit ciphertext and a hand-rotated service account, then stops and starts the server and both containers,
# and checks that the data is still there, unseal worked and init.sh overwrote nothing. Also an expired AppRole secret_id and
# the sealed window at start (fail closed). Prints PASS/FAIL lines; exit 1 on FAIL. Cleans up after itself.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
image="$(grep -m1 'image: openbao/openbao' "$root/docker-compose.yml" | sed 's/.*image: //')"
net="m5-baop-$$"
server="$net-server"
unseal="$net-unseal"
data="$net-data"
keys="$(mktemp -d "${TMPDIR:-/tmp}/m5-baop-keys.XXXXXX")"
token="test-root-$$"
failed=0

cleanup() {
  docker rm -f "$server" "$unseal" >/dev/null 2>&1 || true
  docker network rm "$net" >/dev/null 2>&1 || true
  docker volume rm "$data" >/dev/null 2>&1 || true
  docker run --rm -u 0 -v "$keys:/keys" --entrypoint /bin/sh "$image" -c 'rm -rf /keys/* /keys/.[!.]*' >/dev/null 2>&1 || true
  rmdir "$keys" >/dev/null 2>&1 || true
}
trap cleanup EXIT
check() { if [ "$2" = ok ]; then echo "PASS $1"; else echo "FAIL $1"; failed=1; fi; }
verdict() { if "${@:2}"; then check "$1" ok; else check "$1" fail; fi; }

bao() { docker run --rm -i --network "$net" -e BAO_ADDR=http://openbao:8200 -e BAO_TOKEN="$1" --entrypoint bao "$image" "${@:2}"; }
# The keys directory is a bind mount, as .secrets/openbao is in the stack.
in_keys() { docker run --rm -u 0 -v "$keys:/keys" --entrypoint /bin/sh "$image" -c "$1"; }

start_server() {
  docker run -d --name "$server" --network "$net" --network-alias openbao -e BAO_ADDR=http://127.0.0.1:8200 \
    -v "$data:/openbao/file" -v "$root/openbao/server.hcl:/openbao/server.hcl:ro" -v "$root/openbao/audit.hcl:/openbao/audit.hcl:ro" \
    "$image" server -config=/openbao/server.hcl -config=/openbao/audit.hcl >/dev/null
}
start_unseal() {
  docker run -d --name "$unseal" --network "$net" -u 0:0 -e BAO_ADDR=http://openbao:8200 -e BAO_ROOT_TOKEN="$token" -e BAO_ALLOW_REINIT="${ALLOW_REINIT:-0}" \
    -v "$keys:/keys" -v "$root/openbao/unseal.sh:/unseal.sh:ro" --entrypoint /bin/sh "$image" /unseal.sh >/dev/null
}
run_init() { # extra docker env flags
  docker run --rm --network "$net" -v "$root/openbao/init.sh:/init.sh:ro" -e BAO_ADDR=http://openbao:8200 -e BAO_ROOT_TOKEN="$token" \
    -e BAO_BROKER_ROLE_ID=broker-role -e BAO_BROKER_SECRET_ID="${SECRET_ID:-broker-secret-id}" -e GITLAB_OAUTH_CLIENT_SECRET=gitlab-test \
    -e SERVICEDESK_API_KEY=sd-from-env -e KC_CLIENT_MCP_CREDENTIAL_BROKER_SECRET=kc-test "$@" --entrypoint /bin/sh "$image" /init.sh
}
unsealed() { bao "" status >/dev/null 2>&1 && bao "$token" token lookup >/dev/null 2>&1; }
wait_unsealed() { for _ in $(seq "${1:-60}"); do unsealed && return 0; sleep 1; done; return 1; }
broker_login() { printf '%s' "${SECRET_ID:-broker-secret-id}" | bao "" write -field=token auth/approle/login role_id=broker-role secret_id=- 2>/dev/null; }

docker network create "$net" >/dev/null
docker volume create "$data" >/dev/null
start_server
start_unseal
verdict "first start: initialised and unsealed by openbao-unseal" wait_unsealed
[ "$(bao "$token" token lookup -field=policies 2>/dev/null | tr -d '[]\n')" = root ] && check "stack token (fixed id) is a root token" ok || check "stack token (fixed id) is a root token" fail
perm="$(in_keys 'stat -c %a /keys/unseal-key /keys/root-token' | tr '\n' ' ')"
[ "$perm" = "600 600 " ] && check "unseal-key and root-token have mode 600" ok || check "unseal-key and root-token have mode 600 (got: $perm)" fail

run_init >/dev/null && check "init.sh on the fresh instance" ok || check "init.sh on the fresh instance" fail
printf 'user-pat-1' | bao "$token" kv put secret/mcp/connections/sub-1/jira pat=- >/dev/null
cipher="$(printf '%s' 'seed-plain' | base64 | bao "$token" write -field=ciphertext transit/encrypt/mcp-authenticators plaintext=- 2>/dev/null)"
printf 'rotated-by-hand' | bao "$token" kv put secret/mcp/service-accounts/servicedesk value=- >/dev/null
expiry_before="$(printf '%s' broker-secret-id | bao "$token" write -field=expiration_time auth/approle/role/mcp-credential-broker/secret-id/lookup secret_id=- 2>/dev/null)"
[ -n "$cipher" ] && [ -n "$expiry_before" ] && check "test data written (secret, ciphertext, service account, secret_id expiry $expiry_before)" ok \
  || check "test data written" fail

survives() { # label
  [ "$(bao "$token" kv get -field=pat secret/mcp/connections/sub-1/jira 2>/dev/null)" = user-pat-1 ] && check "$1: user secret is there" ok || check "$1: user secret is there" fail
  [ "$(printf '%s' "$cipher" | bao "$token" write -field=plaintext transit/decrypt/mcp-authenticators ciphertext=- 2>/dev/null | base64 -d 2>/dev/null)" = seed-plain ] \
    && check "$1: Transit ciphertext decrypts with the old key" ok || check "$1: Transit ciphertext decrypts with the old key" fail
}
init_keeps() { # label
  run_init >/dev/null && check "$1: init.sh runs to the end" ok || check "$1: init.sh runs to the end" fail
  [ "$(bao "$token" kv get -field=value secret/mcp/service-accounts/servicedesk 2>/dev/null)" = rotated-by-hand ] \
    && check "$1: init.sh left the service account alone" ok || check "$1: init.sh left the service account alone" fail
  [ "$(bao "$token" read -field=latest_version transit/keys/mcp-authenticators 2>/dev/null)" = 1 ] \
    && [ "$(bao "$token" read -field=latest_version transit/keys/mcp-credential-broker 2>/dev/null)" = 1 ] \
    && check "$1: Transit keys are still version 1" ok || check "$1: Transit keys are still version 1" fail
  [ "$(printf '%s' broker-secret-id | bao "$token" write -field=expiration_time auth/approle/role/mcp-credential-broker/secret-id/lookup secret_id=- 2>/dev/null)" = "$expiry_before" ] \
    && check "$1: AppRole secret_id kept its expiry" ok || check "$1: AppRole secret_id kept its expiry" fail
  [ -n "$(broker_login)" ] && check "$1: broker AppRole logs in" ok || check "$1: broker AppRole logs in" fail
}

docker stop "$server" >/dev/null
docker start "$server" >/dev/null
verdict "restart of the server alone: unsealed again by the running sidecar" wait_unsealed
survives "server restart"
init_keeps "server restart"

docker stop "$server" "$unseal" >/dev/null
docker start "$server" "$unseal" >/dev/null
verdict "stop and start of both containers: unsealed" wait_unsealed
survives "stop/start"
init_keeps "stop/start"

docker rm -f "$server" >/dev/null
start_server
verdict "container removed and created again on the same volume: unsealed" wait_unsealed
survives "recreate"
init_keeps "recreate"

docker stop "$server" "$unseal" >/dev/null
docker start "$server" >/dev/null
sealed=0
for _ in $(seq 30); do
  rc=0; bao "" status >/dev/null 2>&1 || rc=$?
  [ "$rc" = 2 ] && { sealed=1; break; }
  sleep 1
done
[ "$sealed" = 1 ] && check "server without the sidecar stays sealed" ok || check "server without the sidecar stays sealed" fail
login_http="$(docker run --rm --network "$net" --entrypoint /bin/sh "$image" -c \
  "printf '{\"role_id\":\"broker-role\",\"secret_id\":\"broker-secret-id\"}' | wget -q -O /dev/null --post-file=/dev/stdin --header 'Content-Type: application/json' http://openbao:8200/v1/auth/approle/login 2>&1" || true)"
case "$login_http" in *503*) check "sealed: broker login is refused with 503 (fail closed)" ok ;; *) check "sealed: broker login is refused with 503 (got: $login_http)" fail ;; esac
start_unseal_again=0
docker start "$unseal" >/dev/null && start_unseal_again=1
[ "$start_unseal_again" = 1 ] && verdict "sidecar started after the server: unsealed" wait_unsealed
survives "late unseal"
[ -n "$(broker_login)" ] && check "late unseal: broker logs in without a new init" ok || check "late unseal: broker logs in without a new init" fail

key="$(in_keys 'cat /keys/unseal-key')"
rootgen="$(in_keys 'cat /keys/root-token')"
leaks=0
for log in "$(docker logs "$server" 2>&1)" "$(docker logs "$unseal" 2>&1)"; do
  printf '%s' "$log" | grep -qF -e "$key" -e "$rootgen" && leaks=1
done
[ "$leaks" = 0 ] && check "neither the unseal key nor the root token is in the logs" ok || check "neither the unseal key nor the root token is in the logs" fail

SECRET_ID=short-lived
run_init -e BAO_SECRET_ID_TTL=20s >/dev/null
[ -n "$(broker_login)" ] && check "short-lived secret_id (20 s) logs in" ok || check "short-lived secret_id (20 s) logs in" fail
exp1="$(printf '%s' short-lived | bao "$token" write -field=expiration_time auth/approle/role/mcp-credential-broker/secret-id/lookup secret_id=- 2>/dev/null)"
sleep 8
renew_out="$(run_init -e BAO_SECRET_ID_TTL=20s)"
printf '%s' "$renew_out" | grep -q 'less than half' && check "init.sh renews a secret_id with less than half of its TTL left" ok || check "init.sh renews a secret_id with less than half of its TTL left" fail
exp2="$(printf '%s' short-lived | bao "$token" write -field=expiration_time auth/approle/role/mcp-credential-broker/secret-id/lookup secret_id=- 2>/dev/null)"
[ -n "$exp2" ] && [[ "$exp2" > "$exp1" ]] && check "the renewed secret_id expires later ($exp1 -> $exp2)" ok || check "the renewed secret_id expires later ($exp1 -> $exp2)" fail
sleep 24
[ -z "$(broker_login)" ] && check "the short-lived secret_id has expired" ok || check "the short-lived secret_id has expired" fail
run_init -e BAO_SECRET_ID_TTL=720h >/dev/null && [ -n "$(broker_login)" ] \
  && check "init.sh recreates an expired secret_id and the broker logs in" ok || check "init.sh recreates an expired secret_id and the broker logs in" fail

# Old token, new instance (what a running broker meets after make openbao-migrate): the token is refused (403), a fresh AppRole login works.
SECRET_ID=broker-secret-id
run_init >/dev/null
old_token="$(broker_login)"
[ -n "$old_token" ] && [ "$(bao "$old_token" kv list -format=json secret/mcp/connections 2>&1 | grep -c 'sub-1')" -ge 1 ] && check "before the new instance: broker token reads the connections" ok || check "before the new instance: broker token reads the connections" fail

# Init is refused while an unseal-key exists and the server is empty (another volume), and the key file is not touched.
docker rm -f "$server" "$unseal" >/dev/null
docker volume rm "$data" >/dev/null
docker volume create "$data" >/dev/null
key_sum_before="$(in_keys 'cksum /keys/unseal-key')"
start_server
start_unseal
sleep 24
[ "$(in_keys 'cksum /keys/unseal-key')" = "$key_sum_before" ] && docker logs "$unseal" 2>&1 | grep -q 'REFUSING TO INIT' \
  && check "empty server with an existing unseal-key: init refused, key file untouched" ok || check "empty server with an existing unseal-key: init refused, key file untouched" fail
[ "$(bao "" status 2>&1 | grep -c 'Initialized *false')" -ge 1 ] && check "the refused server stays uninitialised" ok || check "the refused server stays uninitialised" fail

docker rm -f "$unseal" >/dev/null
ALLOW_REINIT=1 start_unseal
verdict "BAO_ALLOW_REINIT=1: new instance initialised and unsealed" wait_unsealed
[ "$(in_keys 'ls /keys | grep -c "^unseal-key.bak-"')" = 1 ] && [ "$(in_keys 'ls /keys | grep -c "^root-token.bak-"')" = 1 ] \
  && check "the old unseal-key and root-token are kept as .bak-<date>" ok || check "the old unseal-key and root-token are kept as .bak-<date>" fail
[ "$(in_keys 'stat -c %a /keys/unseal-key.bak-*' | sort -u | tr '\n' ' ')" = "600 " ] && check "the .bak files have mode 600" ok || check "the .bak files have mode 600" fail
if bao "$old_token" kv list secret/mcp/connections >/dev/null 2>&1; then check "old broker token is refused by the new instance (403)" fail; else check "old broker token is refused by the new instance (403)" ok; fi
run_init >/dev/null
[ -n "$(broker_login)" ] && check "after init.sh on the new instance the broker logs in again with the same role_id and secret_id" ok || check "after init.sh on the new instance the broker logs in again with the same role_id and secret_id" fail
if bao "$(broker_login)" kv get secret/mcp/connections/sub-1/jira >/dev/null 2>&1; then check "the new instance has no user secrets (they are lost, as documented)" fail; else check "the new instance has no user secrets (they are lost, as documented)" ok; fi
exit "$failed"
