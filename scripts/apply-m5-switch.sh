#!/usr/bin/env bash
# Applies the M5 switch (mcp-contracts 5.1.0) to the RUNNING M4 stack without down/clean/reset: gateway and broker become two
# instances each behind balancers, /mcp replaces the saved links, the components are recreated from the images in m5.env in
# dependency order. PostgreSQL, Redis (AOF volume), OpenBao data, Keycloak and the Authenticator registrations stay.
# Description of what is needed and what happens to sessions and data: docs/m5.md.
#
#   scripts/apply-m5-switch.sh --check    only the preconditions below, changes nothing (also `make m5-switch CHECK=1`)
#   scripts/apply-m5-switch.sh            the switch; DO NOT RUN without the order of the coordinator (people use the M4 stack)
#
# Preconditions (the script stops before touching anything when one is missing):
#   - the M4 layer is active (.env has M4_ACTIVE=1);
#   - m5.env sets the nine component images to tags starting with m5- and they exist locally;
#   - the pinned nginx image of the balancers exists locally;
#   - the dev- public keys of ws-09 and a manifest built from each server repository exist (as for m4-switch).
# Order: backup of the saved links -> C2 keys -> broker 1, broker 2, broker balancer -> signal to ws-09 and wait for the 5.1.0
# bundle -> MCP servers -> gateway 1, gateway 2, gateway balancer -> registration by CI -> BFF and web -> proxy -> mocks -> checks.
# Rollback is not automatic: the images that ran before are in out/m5-previous-images.txt, the links in out/m5-virtual-servers.sql;
# the gateway keeps the accepted policy revision (rollback protection), so a 4.x gateway needs a 4.x bundle published again.
# RESTART_GITLAB_MOCK=1 also restarts the GitLab mock (it keeps OAuth tokens in memory: every GitLab connection must be made again).
# M5_PUBLISHED=1 skips waiting for the bundle.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
compose="$root/scripts/compose.sh"
# The prospective layers are what the checks look at; .env gets M5_ACTIVE only after them.
export M4_ACTIVE=1 M5_ACTIVE=1
c=("$compose" --profile full --profile m2)
check_only=0
[ "${1:-}" = "--check" ] && check_only=1

fail() { echo "apply-m5-switch: $*" >&2; exit 1; }

[ "$(grep -s '^M4_ACTIVE=' "$root/.env" | cut -d= -f2-)" = 1 ] || fail "the M4 layer is not active (make m4-switch first)"

required="MCP_GATEWAY_IMAGE MCP_CREDENTIAL_BROKER_IMAGE MCP_PORTAL_BFF_IMAGE MCP_PORTAL_WEB_IMAGE MCP_JIRA_IMAGE MCP_CONFLUENCE_IMAGE MCP_GITLAB_IMAGE MCP_SERVICEDESK_IMAGE MCP_OPENSEARCH_IMAGE"
for var in $required; do
  value="$(grep "^$var=" "$root/m5.env" | tail -1 | cut -d= -f2- || true)"
  [ -n "$value" ] || fail "m5.env does not set $var (the M5 build has not been announced)"
  case "$value" in *:m5-*) ;; *) fail "$var=$value does not have an m5- tag" ;; esac
done
config="$("${c[@]}" config --format json)"
images="$(python3 -c '
import json, sys
for name, svc in sorted(json.load(sys.stdin)["services"].items()):
    if name in ("mcp-gateway", "mcp-gateway-2", "mcp-credential-broker", "mcp-credential-broker-2", "mcp-jira", "mcp-confluence",
                "mcp-gitlab", "mcp-servicedesk", "mcp-opensearch", "portal-bff", "portal-web", "gateway-lb", "broker-lb", "proxy"):
        print(name, svc["image"])' <<<"$config")"
while read -r service image; do
  docker image inspect "$image" >/dev/null 2>&1 || fail "$service: image $image does not exist locally (make images / make images-m2)"
  case "$image" in
    *:m4-*|*:m3-*|*:m2-*|*:m1-*) fail "$service: image $image is not an M5 image" ;;
  esac
done <<<"$images"
echo "apply-m5-switch: images ok"

for key in dev-policies-main dev-policies-overrides; do
  [ -f "$root/../mcp-policies/keys/dev/$key.public.pem" ] || fail "missing ../mcp-policies/keys/dev/$key.public.pem (ws-09)"
done
for server in mcp-jira mcp-confluence mcp-gitlab mcp-servicedesk mcp-opensearch; do
  manifest="${MANIFEST_DIR:+$MANIFEST_DIR/$server.json}"
  manifest="${manifest:-$root/../mcp-server-${server#mcp-}/mcp-app/target/mcp-manifest.json}"
  [ -f "$manifest" ] || fail "$server: no manifest at $manifest (build the server; the registry is filled by the CI PUT)"
done
python3 -I "$root/scripts/check-compose.py" | tail -1 | grep -q ' 0 failed' || fail "scripts/check-compose.py reports failures"
echo "apply-m5-switch: dev keys, manifests and compose layers ok"
[ "$check_only" = 1 ] && { echo "apply-m5-switch: --check passed, nothing changed"; exit 0; }

mkdir -p "$root/out" "$root/artifacts/packages"
docker ps --format '{{.Names}} {{.Image}}' | grep '^mcp-dev-' > "$root/out/m5-previous-images.txt" || true

if "${c[@]}" exec -T postgres psql -U gateway -d gateway -At -c "select to_regclass('public.virtual_servers')" | grep -q virtual_servers; then
  "${c[@]}" exec -T postgres pg_dump -U gateway -d gateway --data-only --table=virtual_servers > "$root/out/m5-virtual-servers.sql"
  echo "apply-m5-switch: saved links backed up to out/m5-virtual-servers.sql ($("${c[@]}" exec -T postgres psql -U gateway -d gateway -At -c 'select count(*) from virtual_servers') rows)"
else
  echo "apply-m5-switch: no table virtual_servers, nothing to back up"
fi
latest_before="$(curl -s "http://localhost:$(grep '^POLICIES_PORT=' "$root/.env" | cut -d= -f2-)/policies/main/latest.json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("revision",""))' || true)"

"$root/scripts/gen-c2-keys.sh"
grep -q '^M5_ACTIVE=' "$root/.env" && sed -i.bak 's/^M5_ACTIVE=.*/M5_ACTIVE=1/' "$root/.env" && rm -f "$root/.env.bak" \
  || printf '\n# M5 layer of compose (docker-compose.m5.yml, m5.env), set by make m5-switch.\nM5_ACTIVE=1\n' >> "$root/.env"
echo "apply-m5-switch: M5_ACTIVE=1 in .env (two instances, balancers, /mcp routes, M5 images)"

"${c[@]}" up -d --no-deps --wait mcp-credential-broker
"${c[@]}" up -d --no-deps --wait mcp-credential-broker-2
"${c[@]}" up -d --no-deps --wait broker-lb

echo "apply-m5-switch: SIGNAL to ws-09: components of 5.1.0 run with the dev- keys; publish the 5.1.0 bundle (.ci/switch-stack.sh --confirm); latest revision now: ${latest_before:-unknown}"
if [ "${M5_PUBLISHED:-0}" != 1 ]; then
  deadline=$((SECONDS + 600))
  while :; do
    latest="$(curl -s "http://localhost:$(grep '^POLICIES_PORT=' "$root/.env" | cut -d= -f2-)/policies/main/latest.json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("revision",""))' || true)"
    [ -n "$latest" ] && [ "$latest" != "$latest_before" ] && break
    [ "$SECONDS" -lt "$deadline" ] || fail "no new main bundle within 10 minutes"
    sleep 5
  done
  python3 "$root/scripts/m4.py" wait-bundle 120 || fail "the new bundle does not verify with the dev- key"
fi

"${c[@]}" up -d --no-deps --wait server-state-init mcp-jira mcp-confluence mcp-gitlab mcp-servicedesk mcp-opensearch
"${c[@]}" up -d --no-deps --wait mcp-gateway
"${c[@]}" up -d --no-deps --wait mcp-gateway-2
"${c[@]}" up -d --no-deps --wait gateway-lb
"$root/scripts/registry-register.sh"
"${c[@]}" up -d --no-deps --wait portal-bff portal-web
"${c[@]}" up -d --no-deps --wait proxy

mocks="confluence-mock servicedesk-mock opensearch-mock"
[ "${RESTART_GITLAB_MOCK:-0}" = 1 ] && mocks="$mocks gitlab-mock"
"${c[@]}" restart $mocks >/dev/null
echo "apply-m5-switch: mocks restarted with the controlled latency and 429 (scripts/mock-fault.py): $mocks"

for service in mcp-gateway mcp-gateway-2; do
  log="$("${c[@]}" logs "$service" 2>&1)"
  grep -q 'event=devstack_profile' <<<"$log" || fail "$service did not log event=devstack_profile"
  ! grep -q 'No gateway.internal-jwt.keys-file' <<<"$log" || fail "$service generated its own C2 keys instead of reading the shared file"
done
for port in "$(grep '^GATEWAY_1_PORT=' "$root/m5.env" | cut -d= -f2-)" "$(grep '^GATEWAY_2_PORT=' "$root/m5.env" | cut -d= -f2-)" \
            "$(grep '^GATEWAY_PORT=' "$root/.env" | cut -d= -f2-)"; do
  curl -sf "http://127.0.0.1:$port/.well-known/oauth-protected-resource/mcp" | python3 -c '
import json, sys
resource = json.load(sys.stdin)["resource"]
sys.exit(0 if resource.endswith("/mcp") and "?" not in resource else 1)' || fail "port $port: no protected resource metadata of /mcp"
done
echo "apply-m5-switch: both gateway instances and the balancer serve the metadata of /mcp"
"$root/scripts/proxy-test.sh" | tail -2 || echo "apply-m5-switch: proxy-test reported failures (see above)"
python3 "$root/scripts/m4.py" run || echo "apply-m5-switch: make m4 reported failures (see above)"
echo "apply-m5-switch: done; now: make m1 (through the balancer), mcp-e2e scenarios of points A and B, the kill of one instance"
