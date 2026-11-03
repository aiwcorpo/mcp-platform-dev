#!/usr/bin/env bash
# Moves the stack to the images of m6b.env (M6 stage 3, contracts 6.2.0), one instance at a time, each followed by a health check:
#   broker-1, broker-2, gateway-1, gateway-2, (balancer config reloaded), mcp-jira, mcp-confluence, mcp-gitlab, mcp-opensearch, portal-bff, portal-web.
# Nothing else is recreated (OpenBao, PostgreSQL, Keycloak, Redis, mocks, mcp-servicedesk, balancers, proxy stay as they are); `up --no-deps` names
# the service. The init jobs are never run. New tool definitions of the servers arrive in the registry as pending and are listed at the end;
# approving UPDATE and DESTRUCTIVE tools is a decision of an administrator, not of this script.
#   scripts/apply-m6b-switch.sh check              the plan of the project (what Compose would recreate) and the counts, changes nothing
#   scripts/apply-m6b-switch.sh apply --confirm    M6B_ACTIVE=1 in .env, then the services in turn
#   scripts/apply-m6b-switch.sh rollback --confirm M6B_ACTIVE=0: gateway and broker back to m6.env, servers and portal to m5.env
#   scripts/apply-m6b-switch.sh pending            the tools waiting for approval (SELECT only)
# Before a rollback read the README of the gateway ("Przejscie z M5 na M6 i cofniecie"): the old gateway ignores kill switches, so the rollback
# is refused while one is active (FORCE=1 overrides), and it does not know adapter servers. V8 and V9 stay in the database; the old image ignores them.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
project="$(grep -s '^COMPOSE_PROJECT_NAME=' "$root/.env" | cut -d= -f2- || true)"
project="${project:-mcp-dev}"
mode="${1:-check}"
order=(mcp-credential-broker mcp-credential-broker-2 mcp-gateway mcp-gateway-2 mcp-jira mcp-confluence mcp-gitlab mcp-opensearch portal-bff portal-web)
compose() { "$root/scripts/compose.sh" --profile full --profile m2 "$@"; }
psql_in() { docker exec "$project-postgres-1" psql -U postgres -d "$1" -tAc "$2"; }
fail() { echo "apply-m6b-switch: $*" >&2; exit 1; }

counts() {
  psql_in broker "select 'connections ' || (select count(*) from connection) || ', authenticators ' || (select count(*) from authenticator)"
}
pending() {
  psql_in gateway "select server_id || ' ' || tool || ' ' || pending_change || ' ' || coalesce(pending_entry->>'risk', '?') from registry_tools where pending_entry is not null order by server_id, tool"
}
container() { echo "$project-$1-1"; }
set_flag() { # 0|1
  if grep -q '^M6B_ACTIVE=' "$root/.env"; then sed -i.bak "s/^M6B_ACTIVE=.*/M6B_ACTIVE=$1/" "$root/.env" && rm -f "$root/.env.bak"
  else printf '\n# M6 stage 3 images (m6b.env), set by scripts/apply-m6b-switch.sh.\nM6B_ACTIVE=%s\n' "$1" >> "$root/.env"; fi
}
expected_image() {
  compose config --format json | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["services"][sys.argv[1]]["image"])' "$1"
}
wait_healthy() {
  for _ in $(seq 120); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null || true)" = healthy ] && return 0
    sleep 2
  done
  return 1
}
unhealthy() { docker ps --format '{{.Names}} {{.Status}}' | grep -E "^$project-" | grep -v '(healthy)' || true; }

reload_balancer() {
  docker exec "$project-gateway-lb-1" nginx -t >/dev/null 2>&1 || fail "the new configuration of the gateway balancer does not pass nginx -t; not reloaded"
  docker exec "$project-gateway-lb-1" nginx -s reload
  echo "apply-m6b-switch: gateway-lb reloaded (502 and 504 pages of BalancerError, read timeout 120 s)"
}

roll() {
  local service image
  for service in "${order[@]}"; do
    image="$(expected_image "$service")"
    echo "apply-m6b-switch: $service -> $image"
    compose up -d --no-deps --wait "$service"
    [ "$(docker inspect -f '{{.Config.Image}}' "$(container "$service")")" = "$image" ] || fail "$service does not run $image"
    wait_healthy "$(container "$service")" || fail "$service is not healthy after the switch; the rest stays as it is (see rollback --confirm)"
    [ -z "$(unhealthy)" ] || fail "something is not healthy after $service: $(unhealthy)"
    echo "apply-m6b-switch: $service healthy, $(counts)"
    if [ "$service" = mcp-gateway-2 ]; then reload_balancer; fi
  done
  echo "apply-m6b-switch: all services done"
}

case "$mode" in
  check)
    echo "now: $(counts)"
    echo "plan of the whole project with M6B_ACTIVE=1 (init jobs are listed too; this script never runs them):"
    M6B_ACTIVE=1 timeout 60 "$root/scripts/compose.sh" --profile full --profile m2 up -d --dry-run 2>&1 | grep -E 'Recreate$' | sort -u || true
    for service in "${order[@]}"; do echo "image of $service now: $(docker inspect -f '{{.Config.Image}}' "$(container "$service")")"; done
    ;;
  apply)
    [ "${2:-}" = --confirm ] || fail "add --confirm"
    [ -z "$(unhealthy)" ] || fail "not everything is healthy before the switch: $(unhealthy)"
    echo "before: $(counts)"
    set_flag 1
    roll
    echo "after:  $(counts)"
    ;;
  rollback)
    [ "${2:-}" = --confirm ] || fail "add --confirm"
    active="$(psql_in gateway "select count(*) from kill_switches" 2>/dev/null || echo unknown)"
    if [ "$active" != 0 ] && [ "${FORCE:-0}" != 1 ]; then
      fail "kill switches active or unreadable ($active): the M5 gateway ignores them. Clear them first, or FORCE=1"
    fi
    echo "before: $(counts)"
    set_flag 0
    roll
    echo "after:  $(counts)"
    ;;
  pending)
    pending
    ;;
  *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
