#!/usr/bin/env bash
# Profile e2e-prod: a gateway outside the devstack profile with a broker stand-in and an MCP server stand-in, in its own compose
# project (mcp-e2e-prod, docker-compose.e2e-prod.yml). It never starts, stops or changes a container of the main stack; the one link is the
# network mcp-dev-core that the key-set front joins. See docs/m6.md.
#
#   scripts/e2e-prod.sh up                  prepare keys and the signed bundle (once), start the project and wait for health
#   scripts/e2e-prod.sh down                stop the project and drop its volumes (database, Redis, gateway state); keys are kept
#   scripts/e2e-prod.sh reset-keys          also forget the generated keys, secrets and bundle (next up makes new ones)
#   scripts/e2e-prod.sh status              containers of the project and the addresses a test needs (JSON on stdout with `info`)
#   scripts/e2e-prod.sh info                addresses and file names as JSON
#   scripts/e2e-prod.sh broker-type [TYPE]  show or set the credential type the broker stand-in returns (any string up to 64 characters)
#   scripts/e2e-prod.sh manifest [dev|production|none]   manifest of the server stand-in with that environment field, on stdout
#   scripts/e2e-prod.sh register [dev|production|none]   PUT of that manifest to the gateway (CN mcp-registry-ci-mcp-jira); prints HTTP status and body
#   scripts/e2e-prod.sh devkey-probe        start a gateway in production mode with dev- keys; PASS when it refuses to start
#   scripts/e2e-prod.sh restart-gateway     restart the gateway container (for a stored manifest that must be checked at start)
#   scripts/e2e-prod.sh logs [SERVICE]      logs of the project (default: the gateway)
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
state="$root/out/e2e-prod"
project=mcp-e2e-prod
openssl_image="alpine/openssl:3.5.9@sha256:1c361a204e9791bec6202bccbd1065eafaa65cb5a1a9a06ae000238569fba6a2"

env_value() { grep -s "^$1=" "$2" | cut -d= -f2- || true; }

compose() {
  local envs=(--env-file "$root/.env" --env-file "$root/.secrets/dev.env")
  if [ "$(env_value M4_ACTIVE "$root/.env")" = 1 ]; then envs+=(--env-file "$root/m4.env"); fi
  if [ "$(env_value M5_ACTIVE "$root/.env")" = 1 ]; then envs+=(--env-file "$root/m5.env"); fi
  envs+=(--env-file "$root/e2e-prod.env")
  [ ! -f "$state/secrets.env" ] || envs+=(--env-file "$state/secrets.env")
  docker compose --project-directory "$root" -p "$project" -f "$root/docker-compose.e2e-prod.yml" --profile e2e-prod "${envs[@]}" "$@"
}

port() { env_value "$1" "$root/e2e-prod.env"; }

prepare_secrets() {
  [ ! -s "$state/secrets.env" ] || return 0
  umask 077
  mkdir -p "$state"
  python3 -I - "$state/secrets.env" <<'PY'
import secrets, sys
names = ("E2E_PROD_DB_PASSWORD", "E2E_PROD_REDIS_PASSWORD", "E2E_PROD_ADMIN_TOKEN", "E2E_PROD_ACTIVITY_CURSOR_KEY", "E2E_PROD_LIST_CURSOR_KEY")
with open(sys.argv[1], "w") as out:
    for name in names:
        out.write(f"{name}={secrets.token_urlsafe(32)}\n")
PY
}

openssl_run() {
  docker run --rm --user "$(id -u):$(id -g)" -v "$state:/work" -w /work --entrypoint openssl "$openssl_image" "$@"
}

prepare_bundle() {
  [ ! -s "$state/policies/main/latest.json" ] || return 0
  local repo source revision archive
  repo="$(env_value POLICY_REPOSITORY_DIR "$root/.env")"
  case "$repo" in /*) ;; *) repo="$root/$repo" ;; esac
  source="$repo/main/latest.json"
  [ -s "$source" ] || { echo "e2e-prod: no bundle at $source (the policy repository of the stack)" >&2; exit 1; }
  revision="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["revision"])' "$source")"
  archive="policies-$revision.tar.gz"
  umask 077
  mkdir -p "$state/keys" "$state/policies/main"
  if [ ! -s "$state/keys/e2e-prod-main.private.pem" ]; then
    openssl_run genpkey -algorithm ED25519 -out keys/e2e-prod-main.private.pem
    openssl_run pkey -in keys/e2e-prod-main.private.pem -pubout -out keys/e2e-prod-main.public.pem
  fi
  cp "$repo/main/$archive" "$state/policies/main/$archive"
  openssl_run pkeyutl -sign -rawin -inkey keys/e2e-prod-main.private.pem -in "policies/main/$archive" -out "policies/main/$archive.sig"
  printf '{"revision": "%s", "url": "%s", "sigUrl": "%s.sig"}\n' "$revision" "$archive" "$archive" > "$state/policies/main/latest.json"
  chmod 644 "$state"/policies/main/* "$state"/keys/*.public.pem
}

prepare_trust() {
  [ ! -s "$state/ca-trust.p12" ] || return 0
  cp "$root/certs/ca/ca.crt" "$state/ca.crt"
  openssl_run pkcs12 -export -nokeys -in ca.crt -jdktrust anyExtendedKeyUsage -name mcp-dev-ca -passout pass:changeit -out ca-trust.p12
  chmod 644 "$state/ca-trust.p12"
}

prepare() {
  docker network inspect mcp-dev-core >/dev/null 2>&1 || { echo "e2e-prod: the network mcp-dev-core does not exist; start the main stack first (make up)" >&2; exit 1; }
  [ -f "$root/.secrets/dev.env" ] || { echo "e2e-prod: run make secrets first" >&2; exit 1; }
  "$root/scripts/gen-certs.sh" >/dev/null
  prepare_secrets
  mkdir -p "$state"
  C2_JWKS_FILE="$state/c2-jwks.json" "$root/scripts/gen-c2-keys.sh" >/dev/null
  prepare_bundle
  prepare_trust
}

admin_token() { env_value E2E_PROD_ADMIN_TOKEN "$state/secrets.env"; }

stub_environment() { # dev|production|none -> manifest of the server stand-in with that environment field
  python3 -I - "$root" "${1:-none}" <<'PY'
import os, sys
root, environment = sys.argv[1:3]
sys.path.insert(0, f"{root}/mocks/mcp")
os.environ.update({"MCP_STUB_SERVER_ID": "mcp-jira", "MCP_STUB_SYSTEM": "jira", "MCP_STUB_TOOLSET": "xh",
                   "MCP_STUB_INTERNAL_URL": "https://mcp-jira.internal:8443/mcp", "MCP_STUB_CREDENTIAL_REQUIRED": "true",
                   "MCP_STUB_CREDENTIAL_TYPES": "user-secret"})
import mcp_stub
sys.exit(mcp_stub.main(["manifest", "--environment", environment]))
PY
}

cmd_info() {
  python3 -I - "$(port E2E_PROD_GATEWAY_PORT)" "$(port E2E_PROD_GATEWAY_INTERNAL_PORT)" "$(port E2E_PROD_BROKER_ADMIN_PORT)" "$state" <<'PY'
import json, sys
gateway, internal, broker, state = sys.argv[1:5]
print(json.dumps({
    "project": "mcp-e2e-prod",
    "gatewayUrl": f"http://localhost:{gateway}",
    "gatewayAdminUrl": f"https://localhost:{internal}",
    "gatewayAdminTlsName": "mcp-gateway.internal",
    "brokerAdminUrl": f"http://localhost:{broker}",
    "brokerAdminTokenFile": f"{state}/secrets.env (E2E_PROD_ADMIN_TOKEN)",
    "containers": {"gateway": "mcp-e2e-prod-gateway-1", "postgres": "mcp-e2e-prod-postgres-1", "redis": "mcp-e2e-prod-redis-1",
                   "broker": "mcp-e2e-prod-broker-1", "server": "mcp-e2e-prod-mcp-jira-1"},
    "registryClientCertificate": "certs/mcp-registry-ci-mcp-jira",
    "tokenIssuer": "the stack's Keycloak (realm corp), tokens for aud mcp-gateway as for the main gateway",
}, indent=2))
PY
}

case "${1:-}" in
  up)
    prepare
    compose up -d --wait
    cmd_info
    ;;
  down)
    compose down -v --remove-orphans
    ;;
  reset-keys)
    compose down -v --remove-orphans
    rm -rf "$state"
    echo "e2e-prod: keys, secrets and the signed bundle removed"
    ;;
  status)
    compose ps -a
    ;;
  info)
    cmd_info
    ;;
  broker-type)
    prepare_secrets
    token="$(admin_token)"
    url="http://localhost:$(port E2E_PROD_BROKER_ADMIN_PORT)/type"
    if [ -n "${2:-}" ]; then
      printf '{"type": "%s"}' "$2" | curl -sS -X PUT -H "Authorization: Bearer $token" -H 'Content-Type: application/json' --data-binary @- "$url"
    else
      curl -sS -H "Authorization: Bearer $token" "$url"
    fi
    echo
    ;;
  manifest)
    stub_environment "${2:-none}"
    ;;
  register)
    manifest="$(mktemp)"
    trap 'rm -f "$manifest"' EXIT
    stub_environment "${2:-none}" > "$manifest"
    GATEWAY_ADMIN_URL="https://localhost:$(port E2E_PROD_GATEWAY_INTERNAL_PORT)" python3 -I "$root/scripts/mcpdev.py" registry-put mcp-jira "$manifest"
    ;;
  devkey-probe)
    prepare
    probes="$(mktemp -d)"
    trap 'rm -rf "$probes"' EXIT
    sed 's/"stack-c2-1"/"dev-stack-c2-1"/' "$state/c2-jwks.json" > "$probes/dev-c2-jwks.json"
    failed=0
    # Same image and settings as the gateway of the project, without the devstack profile, with a dev- key in one place at a time.
    probe() { # label expected-pattern docker-run-args...
      local label="$1" pattern="$2" log status
      shift 2
      log="$(mktemp)"
      set +e
      compose run --rm --no-deps -v /app/state "$@" gateway >"$log" 2>&1
      status=$?
      set -e
      if [ "$status" -ne 0 ] && grep -qiE "$pattern" "$log"; then
        echo "PASS $label: the gateway refuses to start (exit $status)"
        grep -iE "$pattern" "$log" | grep -v '^[[:space:]]*at ' | head -2 | cut -c1-220
      else
        echo "FAIL $label: exit $status"
        grep -v '^[[:space:]]*at ' "$log" | tail -6 | cut -c1-220
        failed=1
      fi
      rm -f "$log"
    }
    probe "dev- key of the policy bundles" 'TEST_KEY_NOT_ALLOWED|published test key' -e MCP_POLICY_MAIN_KEY_ID=dev-policies-main \
      -e MCP_POLICY_STARTUP_TIMEOUT=15s
    probe "dev- key of the internal JWT" 'dev- keys need dev mode' -e MCP_INTERNAL_JWT_ACTIVE_KID=dev-stack-c2-1 \
      -v "$probes/dev-c2-jwks.json:/run/c2/jwks.json:ro"
    exit "$failed"
    ;;
  restart-gateway)
    compose restart gateway
    ;;
  logs)
    compose logs --no-color --tail 200 "${2:-gateway}"
    ;;
  *)
    sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
