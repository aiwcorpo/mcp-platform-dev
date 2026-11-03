#!/usr/bin/env bash
# Moves the stack to the images of m6c.env (contracts 6.3.2, starter 3.0.0-beta-14), each step followed by a health check:
#   mocks that changed (jira-mock, confluence-mock, opensearch-mock; the GitLab mock holds OAuth state and is never restarted),
#   broker-1, broker-2, gateway-2 stopped, gateway-1, gateway-2, mcp-jira, mcp-confluence, mcp-gitlab, mcp-opensearch, mcp-servicedesk, portal-bff, portal-web.
# Why the gateways go in that order: image m6-dd9769b runs migration V10 (adapter classifications become void and the registry counter moves). An old
# instance that keeps running on the schema after V10 does not reload the registry until it restarts or approves something that moves the counter
# (README of mcp-gateway), and V10 runs only once, so what that instance writes meanwhile is not voided. Hence the old instance 2 is stopped first, instance 1
# is replaced (V10 runs on its start), and only then instance 2 starts, on the new image. The price is a gap: from the stop of instance 2 until instance 1 is
# healthy no gateway instance serves (the gateway balancer answers 502 for roughly the start time of one gateway, about 10 to 20 s, open streams are cut).
# The mocks go first so that the servers' first version probe already finds a version. Nothing else is recreated (OpenBao, PostgreSQL, Keycloak, Redis,
# the balancers and the proxy stay as they are); `up --no-deps` names the service. The init jobs are never run.
#   scripts/apply-m6c-switch.sh check              the counts and the images now, changes nothing
#   scripts/apply-m6c-switch.sh apply --confirm    M6C_ACTIVE=1 in .env, then the services in turn
#   scripts/apply-m6c-switch.sh pending            the tools waiting for approval (SELECT only)
# After V10 the script writes out/m6c.applied (outside git); scripts/compose.sh then refuses to create containers without the M6C layer.
# When the schema is already at 10 (a later image without a migration) the gateways are replaced one by one without the stop. SKIP_MOCKS=1 leaves the mocks alone.
# There is no rollback: V10 stays in the database and the m6b gateway would serve adapter rows that V10 voided. Going back is a decision of a human.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=m6-layers.sh
. "$root/scripts/m6-layers.sh"
project="$(grep -s '^COMPOSE_PROJECT_NAME=' "$root/.env" | cut -d= -f2- || true)"
project="${project:-mcp-dev}"
unset M6_ACTIVE M6B_ACTIVE M6C_ACTIVE   # a value exported in the shell would win over .env in scripts/compose.sh
mode="${1:-check}"
mocks=(jira-mock confluence-mock opensearch-mock)
first=(mcp-credential-broker mcp-credential-broker-2)
last=(mcp-jira mcp-confluence mcp-gitlab mcp-opensearch mcp-servicedesk portal-bff portal-web)
compose() { "$root/scripts/compose.sh" --profile full --profile m2 "$@"; }
psql_in() { docker exec "$project-postgres-1" psql -U postgres -d "$1" -tAc "$2"; }
fail() {
  echo "apply-m6c-switch: $*" >&2
  if [ "${flag_set:-0}" = 1 ]; then
    if v10_applied --check-db; then
      echo "apply-m6c-switch: AFTER V10: the flag M6C_ACTIVE=1 stays, older images must not run on this schema; fix the failed service and run apply --confirm again" >&2
    else
      echo "apply-m6c-switch: BEFORE V10: the schema is still below 10; the flag can be taken back (M6C_ACTIVE=0 in .env) and the stack stays on m6b images" >&2
    fi
  fi
  exit 1
}
container() { echo "$project-$1-1"; }

counts() {
  psql_in broker "select 'connections ' || (select string_agg(state || '=' || n, ', ' order by state) from (select state, count(*) n from connection group by state) c) || ', authenticators ' || (select count(*) from authenticator)"
}
pending() {
  psql_in gateway "select server_id || ' ' || tool || ' ' || pending_change || ' ' || coalesce(pending_entry->>'risk', '?') from registry_tools where pending_entry is not null order by server_id, tool"
}
set_flag() {
  if grep -q '^M6C_ACTIVE=' "$root/.env"; then sed -i.bak "s/^M6C_ACTIVE=.*/M6C_ACTIVE=$1/" "$root/.env" && rm -f "$root/.env.bak"
  else printf '\n# Images on contracts 6.3.2 (m6c.env), set by scripts/apply-m6c-switch.sh.\nM6C_ACTIVE=%s\n' "$1" >> "$root/.env"; fi
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
flyway_max() { psql_in gateway "select max(version::int) from flyway_schema_history where success"; }

restart_mock() {
  local mock="$1"
  docker restart "$(container "$mock")" >/dev/null
  wait_healthy "$(container "$mock")" || fail "$mock is not healthy after the restart"
  echo "apply-m6c-switch: $mock restarted, healthy"
}

switch() {
  local service="$1" image
  image="$(expected_image "$service")"
  echo "apply-m6c-switch: $service -> $image"
  compose up -d --no-deps --wait "$service"
  [ "$(docker inspect -f '{{.Config.Image}}' "$(container "$service")")" = "$image" ] || fail "$service does not run $image"
  wait_healthy "$(container "$service")" || fail "$service is not healthy after the switch; the rest stays as it is"
  [ -z "$(unhealthy)" ] || fail "something is not healthy after $service: $(unhealthy)"
  echo "apply-m6c-switch: $service healthy, $(counts)"
}

roll() {
  local service want
  if [ "${SKIP_MOCKS:-0}" != 1 ]; then for service in "${mocks[@]}"; do restart_mock "$service"; done; fi
  for service in "${first[@]}"; do switch "$service"; done
  want="$(expected_image mcp-gateway)"
  if [ "$(flyway_max)" -ge 10 ]; then
    echo "apply-m6c-switch: schema is already at $(flyway_max), no migration is pending: the gateways are replaced one by one without stopping instance 2 first"
  else
    echo "apply-m6c-switch: stopping mcp-gateway-2 (old image) before V10"
    compose stop mcp-gateway-2
  fi
  switch mcp-gateway
  [ "$(flyway_max)" -ge 10 ] || fail "migration V10 is not in the schema history after mcp-gateway started"
  echo "apply-m6c-switch: schema at $(flyway_max); starting mcp-gateway-2 on the new image"
  [ "$(psql_in gateway "select count(*) from flyway_schema_history where version = '10' and success")" = 1 ] || fail "V10 is missing or failed"
  mkdir -p "$root/out"
  printf 'V10 applied by apply-m6c-switch.sh at %s, gateway %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(expected_image mcp-gateway)" > "$(m6c_marker)"
  switch mcp-gateway-2
  for service in "${last[@]}"; do switch "$service"; done
  echo "apply-m6c-switch: all services done"
}

case "$mode" in
  check)
    echo "now: $(counts)"
    echo "flyway max of the gateway schema: $(flyway_max)"
    for service in "${mocks[@]}" "${first[@]}" mcp-gateway mcp-gateway-2 "${last[@]}"; do
      echo "$service: $(docker inspect -f '{{.Config.Image}}' "$(container "$service")")"
    done
    ;;
  apply)
    [ "${2:-}" = --confirm ] || fail "add --confirm"
    grep -q '^M6B_ACTIVE=1' "$root/.env" || fail "M6B_ACTIVE=1 is not set in .env; .env is left as it is"
    [ -z "$(unhealthy)" ] || fail "not everything is healthy before the switch: $(unhealthy)"
    while IFS= read -r image; do
      docker image inspect "$image" >/dev/null 2>&1 || fail "image $image of m6c.env is not present locally; nothing was changed"
    done < <(sed -n 's/^[A-Z_]*IMAGE=//p' "$root/m6c.env")
    echo "before: $(counts)"
    set_flag 1
    flag_set=1
    roll
    echo "after:  $(counts)"
    ;;
  pending)
    pending
    ;;
  *) grep -E '^#   scripts/' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
