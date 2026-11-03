#!/usr/bin/env bash
# Moves the gateway and broker instances to the images of m6.env, one instance at a time, each followed by a health check:
# broker-1, broker-2, gateway-1, gateway-2. Nothing else is recreated (OpenBao, PostgreSQL, Keycloak, Redis, mocks, servers, balancers stay
# as they are); `up --no-deps` names the instance, and the Compose plan of the whole project is shown first so that it can be compared.
#   scripts/apply-m6-switch.sh check              the plan (which containers would be recreated) and the counts, changes nothing
#   scripts/apply-m6-switch.sh apply --confirm    M6_ACTIVE=1 in .env, then the four instances in turn
#   scripts/apply-m6-switch.sh rollback --confirm M6_ACTIVE=0, the same four back to the images of m5.env (m5-2110cef broker, m5-b2e25ae gateway)
# No image of the layer has a database migration that the M5 image lacks, so the way back needs no restore. A failed check stops the script at
# that instance; the rest of the stack keeps running on whatever image it has (the balancers send to a healthy instance), decide on the rollback.
# Counts of connections and Authenticators (SELECT count(*) only) are printed before and after.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
project="$(grep -s '^COMPOSE_PROJECT_NAME=' "$root/.env" | cut -d= -f2- || true)"
project="${project:-mcp-dev}"
# shellcheck source=m6-layers.sh
. "$root/scripts/m6-layers.sh"
unset M6_ACTIVE M6B_ACTIVE M6C_ACTIVE   # a value exported in the shell would win over .env in scripts/compose.sh
mode="${1:-check}"
order=(mcp-credential-broker mcp-credential-broker-2 mcp-gateway mcp-gateway-2)
compose() { "$root/scripts/compose.sh" --profile full --profile m2 "$@"; }
fail() { echo "apply-m6-switch: $*" >&2; exit 1; }

counts() {
  docker exec "$project-postgres-1" psql -U postgres -d broker -tAc \
    "select 'connections ' || (select count(*) from connection) || ', authenticators ' || (select count(*) from authenticator)"
}
container() { echo "$project-$1-1"; }
set_flag() { # 0|1
  if grep -q '^M6_ACTIVE=' "$root/.env"; then sed -i.bak "s/^M6_ACTIVE=.*/M6_ACTIVE=$1/" "$root/.env" && rm -f "$root/.env.bak"
  else printf '\n# M6 images of the gateway and broker (m6.env), set by scripts/apply-m6-switch.sh.\nM6_ACTIVE=%s\n' "$1" >> "$root/.env"; fi
}
expected_image() { # service
  local var=MCP_GATEWAY_IMAGE
  case "$1" in mcp-credential-broker*) var=MCP_CREDENTIAL_BROKER_IMAGE ;; esac
  compose config --format json | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["services"][sys.argv[1]]["image"])' "$1" || fail "no image for $1 ($var)"
}
wait_healthy() {
  for _ in $(seq 90); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null || true)" = healthy ] && return 0
    sleep 2
  done
  return 1
}
unhealthy() { docker ps --format '{{.Names}} {{.Status}}' | grep -E "^$project-" | grep -v '(healthy)' || true; }

roll() {
  local want="$1" service image
  for service in "${order[@]}"; do
    image="$(expected_image "$service")"
    echo "apply-m6-switch: $service -> $image"
    compose up -d --no-deps --wait "$service"
    [ "$(docker inspect -f '{{.Config.Image}}' "$(container "$service")")" = "$image" ] || fail "$service does not run $image"
    wait_healthy "$(container "$service")" || fail "$service is not healthy after the switch; the rest stays as it is, see 'apply-m6-switch.sh rollback --confirm'"
    [ -z "$(unhealthy)" ] || fail "something is not healthy after $service: $(unhealthy)"
    echo "apply-m6-switch: $service healthy, $(counts)"
  done
  echo "apply-m6-switch: M6_ACTIVE=$want, all four instances done"
}

case "$mode" in
  check)
    echo "now: $(counts)"
    echo "plan of the whole project with M6_ACTIVE=1 (the init jobs are shown too; this script names its services and never runs them):"
    # The dry run ends by waiting for the health of simulated containers, so it is cut after 60 s (python, as macOS has no timeout(1)).
    M6_ACTIVE=1 python3 -I -c '
import subprocess, sys
try:
    out = subprocess.run(sys.argv[1:], capture_output=True, text=True, timeout=60)
    print(out.stdout + out.stderr)
except subprocess.TimeoutExpired as e:
    print((e.stdout or b"").decode() + (e.stderr or b"").decode())' "$root/scripts/compose.sh" --profile full --profile m2 up -d --dry-run 2>&1 | grep -E 'Recreate$|Recreate ' | sort -u || true
    for service in "${order[@]}"; do echo "image of $service now: $(docker inspect -f '{{.Config.Image}}' "$(container "$service")")"; done
    ;;
  apply)
    [ "${2:-}" = --confirm ] || fail "add --confirm"
    [ -z "$(unhealthy)" ] || fail "not everything is healthy before the switch: $(unhealthy)"
    echo "before: $(counts)"
    set_flag 1
    roll 1
    ;;
  rollback)
    [ "${2:-}" = --confirm ] || fail "add --confirm"
    refuse_when_newer_layers apply-m6-switch M6B_ACTIVE M6C_ACTIVE   # before anything is changed, set_flag included
    echo "before: $(counts)"
    set_flag 0
    roll 0
    ;;
  *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
