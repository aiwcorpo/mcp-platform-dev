#!/usr/bin/env bash
# Applies the M5 switch (mcp-contracts 5.1.0) to the RUNNING M4 stack without down/clean/reset: gateway and broker become two
# instances each behind balancers, /mcp replaces the saved links, the components are recreated from the images in m5.env in
# dependency order. PostgreSQL, Redis (AOF volume), OpenBao data, Keycloak and the Authenticator registrations stay.
# Description of what is needed and what happens to sessions and data: docs/m5.md.
#
#   scripts/apply-m5-switch.sh --check      only the preconditions below, changes nothing (also `make m5-switch CHECK=1`)
#   scripts/apply-m5-switch.sh --confirm    the switch (`make m5-switch CONFIRM=1`); DO NOT RUN without the order of the coordinator
#   RESUME=1 scripts/apply-m5-switch.sh --confirm   continue an interrupted switch with its original backup
#
# Preconditions (the script stops before touching anything when one is missing):
#   - the M4 layer is active (.env has M4_ACTIVE=1) and no earlier switch is left half done (out/m5-switch.state);
#   - m5.env sets the nine component images to tags starting with m5- and they exist locally;
#   - the pinned nginx image of the balancers exists locally;
#   - the dev- public keys of ws-09 and a manifest built from each server repository exist (as for m4-switch).
# Before the first change: a timestamped backup in out/m5-backup-<time>/ (full dumps of the gateway and broker databases, the
# table of saved links, flyway versions, running images, .env, the revision of the policy bundle, connection counts of the four
# test accounts). A failure at any later step prints the step, the command and the way back:
#   scripts/rollback-m5.sh out/m5-backup-<time>          (tested part: scripts/rollback-test.sh)
# Order: backup -> OpenBao init (secret_id of the broker) -> C2 keys -> Redis ACL -> broker 1, broker 2, broker balancer -> signal
# to ws-09 and wait for the 5.x bundle -> policy server (access log) -> MCP servers -> gateway 1, gateway 2 (state copied from 1), gateway balancer -> registration
# by CI -> BFF and web -> proxy -> mocks -> checks (proxy tests, make m4, /mcp through the public origin, connection counts).
# Any failed check makes the script exit non-zero (the stack stays as it is; decide on the rollback).
# RESTART_GITLAB_MOCK=1 also restarts the GitLab mock (it keeps OAuth tokens in memory: every GitLab connection must be made again).
# M5_PUBLISHED=1 skips waiting for the bundle.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
compose="$root/scripts/compose.sh"
# The prospective layers are what the checks look at; .env gets M5_ACTIVE only after them.
export M4_ACTIVE=1 M5_ACTIVE=1
c=("$compose" --profile full --profile m2)
mode="${1:-}"
case "$mode" in
  --check|--confirm) ;;
  *) echo "usage: scripts/apply-m5-switch.sh --check | --confirm   (the switch needs --confirm; see the header)" >&2; exit 2 ;;
esac

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
[ "$mode" = "--check" ] && { echo "apply-m5-switch: --check passed, nothing changed"; exit 0; }

pg_exec() { "${c[@]}" exec -T postgres "$@"; }
# shellcheck source=m5-lib.sh
. "$root/scripts/m5-lib.sh"
state="$root/out/m5-switch.state"
step="preparation"
last_command=""
set -E
trap 'last_command=$BASH_COMMAND' ERR
on_exit() {
  local status=$?
  if [ "$status" -ne 0 ] && [ -n "${backup:-}" ] && [ "${switch_phase:-}" = "in-progress" ]; then
    {
      echo "apply-m5-switch: FAILED at step '$step' (exit $status${last_command:+, command: $last_command})"
      echo "apply-m5-switch: the stack is half switched. Way back:   scripts/rollback-m5.sh ${backup#"$root"/}"
      echo "apply-m5-switch: or fix the cause and continue:          RESUME=1 scripts/apply-m5-switch.sh --confirm"
    } >&2
  fi
}
trap on_exit EXIT
state_value() { sed -n "s/^$1=//p" "$state" 2>/dev/null | tail -1; }

mkdir -p "$root/out" "$root/artifacts/packages"
if [ -f "$state" ] && [ "$(state_value phase)" = "in-progress" ]; then
  [ "${RESUME:-0}" = 1 ] || fail "an earlier switch is half done (backup $(state_value backup)): scripts/rollback-m5.sh, or RESUME=1 to continue"
  backup="$(state_value backup)"
  latest_before="$(state_value latest_before)"
  echo "apply-m5-switch: resuming with the backup $backup"
else
  if [ "$(grep -s '^M5_ACTIVE=' "$root/.env" | cut -d= -f2-)" = 1 ]; then
    fail "M5_ACTIVE=1 is already set in .env without a switch in progress; run scripts/rollback-m5.sh first if this is a leftover"
  fi
  step="backup"
  backup="$root/out/m5-backup-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir "$backup"
  docker ps --format '{{.Names}} {{.Image}}' | grep '^mcp-dev-' > "$backup/previous-images.txt"
  cp "$root/.env" "$backup/env.before"
  m5_backup_databases "$backup"
  latest_before="$(python3 "$root/scripts/m5.py" bundle-latest)" || fail "cannot read the latest main bundle from the policy server"
  [ -n "$latest_before" ] || fail "the latest main bundle has no revision"
  python3 "$root/scripts/m5.py" connections snapshot "$backup/connections.json"
  printf 'backup=%s\nphase=in-progress\nlatest_before=%s\n' "$backup" "$latest_before" > "$state"
  echo "apply-m5-switch: backup in $backup"
fi
switch_phase=in-progress

step="openbao-init"
# The AppRole secret_id of the broker lives 168 h from the last init (docs/m5.md): renew it before the new brokers log in.
BAO_RENEW_SECRET_ID=1 "${c[@]}" run --rm --no-deps openbao-init
step="c2 keys"
"$root/scripts/gen-c2-keys.sh"
step="redis acl"
"$root/scripts/redis-sync-acl.sh" >/dev/null
step="M5_ACTIVE in .env"
grep -q '^M5_ACTIVE=' "$root/.env" && sed -i.bak 's/^M5_ACTIVE=.*/M5_ACTIVE=1/' "$root/.env" && rm -f "$root/.env.bak" \
  || printf '\n# M5 layer of compose (docker-compose.m5.yml, m5.env), set by make m5-switch.\nM5_ACTIVE=1\n' >> "$root/.env"
echo "apply-m5-switch: M5_ACTIVE=1 in .env (two instances, balancers, /mcp routes, M5 images)"

step="broker 1"
"${c[@]}" up -d --no-deps --wait mcp-credential-broker
step="broker 2"
"${c[@]}" up -d --no-deps --wait mcp-credential-broker-2
step="broker balancer"
"${c[@]}" up -d --no-deps --wait broker-lb

step="wait for the 5.x bundle"
echo "apply-m5-switch: SIGNAL to ws-09: components of 5.1.0 run with the dev- keys; publish the 5.1.0 bundle (.ci/switch-stack.sh --confirm); latest revision now: $latest_before"
if [ "${M5_PUBLISHED:-0}" != 1 ]; then
  python3 "$root/scripts/m5.py" bundle-wait "$latest_before" 600 || fail "no main bundle with schemaVersion 5.x within 10 minutes"
  python3 "$root/scripts/m4.py" wait-bundle 120 || fail "the new bundle does not verify with the dev- key"
fi

step="policy server access log"
"${c[@]}" up -d --no-deps --wait policies
step="MCP servers"
"${c[@]}" up -d --no-deps --wait server-state-init mcp-jira mcp-confluence mcp-gitlab mcp-servicedesk mcp-opensearch
step="gateway 1"
"${c[@]}" up -d --no-deps --wait mcp-gateway
step="gateway 2 (state of instance 1 copied)"
project="$(grep '^COMPOSE_PROJECT_NAME=' "$root/.env" | cut -d= -f2-)"
docker volume create "${project}_gateway-state-2" >/dev/null
docker run --rm -v "${project}_gateway-state:/from:ro" -v "${project}_gateway-state-2:/to" \
  "$(grep -o 'python:[^ ]*@sha256:[0-9a-f]*' "$root/docker-compose.yml" | head -1)" sh -c 'test -n "$(ls -A /to)" || cp -a /from/. /to/'
"${c[@]}" up -d --no-deps --wait mcp-gateway-2
step="gateway balancer"
"${c[@]}" up -d --no-deps --wait gateway-lb
step="registration by CI"
"$root/scripts/registry-register.sh"
step="BFF and web"
"${c[@]}" up -d --no-deps --wait portal-bff portal-web
step="proxy"
"${c[@]}" up -d --no-deps --wait proxy

step="mocks"
mocks="confluence-mock servicedesk-mock opensearch-mock"
[ "${RESTART_GITLAB_MOCK:-0}" = 1 ] && mocks="$mocks gitlab-mock"
# shellcheck disable=SC2086
"${c[@]}" restart $mocks >/dev/null
echo "apply-m5-switch: mocks restarted with the controlled latency and 429 (scripts/mock-fault.py): $mocks"

step="checks"
failures=()
for service in mcp-gateway mcp-gateway-2; do
  log="$("${c[@]}" logs "$service" 2>&1)"
  grep -q 'event=devstack_profile' <<<"$log" || failures+=("$service did not log event=devstack_profile")
  ! grep -q 'No gateway.internal-jwt.keys-file' <<<"$log" || failures+=("$service generated its own C2 keys instead of reading the shared file")
done
for var in GATEWAY_1_PORT GATEWAY_2_PORT; do
  port="$(grep "^$var=" "$root/m5.env" | cut -d= -f2-)"
  curl -sf "http://127.0.0.1:$port/.well-known/oauth-protected-resource/mcp" >/dev/null || failures+=("$var: no protected resource metadata of /mcp")
done
"$root/scripts/proxy-test.sh" > "$backup/proxy-test.log" 2>&1 || failures+=("proxy-test failed (tail: $(tail -3 "$backup/proxy-test.log" | tr '\n' ' '))")
python3 "$root/scripts/m4.py" run > "$backup/m4.log" 2>&1 || failures+=("make m4 failed (see $backup/m4.log)")
python3 "$root/scripts/m5.py" endpoint-check || failures+=("/mcp through the public origin does not look as the contract says")
python3 "$root/scripts/m5.py" connections compare "$backup/connections.json" || failures+=("connection counts of alice, bob, carol or mallory changed")
if [ "${#failures[@]}" -gt 0 ]; then
  printf 'apply-m5-switch: CHECK FAILED: %s\n' "${failures[@]}" >&2
  echo "apply-m5-switch: the stack is switched but not accepted. Way back: scripts/rollback-m5.sh ${backup#"$root"/}" >&2
  switch_phase=checked
  exit 1
fi
switch_phase="done"
sed -i.bak 's/^phase=.*/phase=done/' "$state" && rm -f "$state.bak"
echo "apply-m5-switch: done (backup $backup); now: make m1 (through the balancer), mcp-e2e scenarios of points A and B, the kill of one instance"
