#!/usr/bin/env bash
# Profile adapter on the main stack: the Jira demo through the adapter of ws-13 in place of the native mcp-jira, READ ONLY (get_issue, search,
# get_all_projects, get_transitions). Never part of `make up*`. Changes what every user of the stack sees while it lasts.
#
#   scripts/adapter.sh status          what runs now (nothing is changed)
#   scripts/adapter.sh on  [--confirm] without --confirm only the checks and the plan; with it: native mcp-jira stopped and removed (its state
#                                      volume stays), the adapter and the foreign server started (own volume mcp-jira-adapter-state), the Jira mock
#                                      connected to the private network of the foreign server, then the gateways one at a time in adapter mode
#   scripts/adapter.sh off [--confirm] the way back, idempotent: gateways one at a time without adapter mode, adapter and foreign server removed,
#                                      the mock disconnected, native mcp-jira started, its manifest registered again (CN mcp-registry-ci-mcp-jira)
# Only on the order of the coordinator. While the adapter exists, `make up*` (scripts/bulk-guard.sh) and `compose up` (scripts/compose.sh) are refused.
# A gateway in adapter mode drops the tools of mcp-jira that are not in the manifest of the adapter, with their approvals: the way back registers
# the native manifest again, so UPDATE and DESTRUCTIVE tools of the native server wait for an administrator again (`apply-m6b-switch.sh pending`).
# The tools of the adapter arrive unclassified and stay disabled until an administrator classifies them (read tools: READ_ONLY, read).
# Interrupted: run `off --confirm` again.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
unset M6_ACTIVE M6B_ACTIVE M6C_ACTIVE
project="$(grep -s '^COMPOSE_PROJECT_NAME=' "$root/.env" | cut -d= -f2- || true)"
project="${project:-mcp-dev}"
compose() { ADAPTER_SWITCH=1 "$root/scripts/compose.sh" --profile full --profile m2 "$@"; }
overlay() { ADAPTER_SWITCH=1 "$root/scripts/compose.sh" -f "$root/docker-compose.adapter.yml" --profile full --profile m2 --profile adapter "$@"; }
psql_gateway() { docker exec "$project-postgres-1" psql -U postgres -d gateway -tAc "$1"; }
fail() { echo "adapter: $*" >&2; exit 1; }
present() { docker inspect "$1" >/dev/null 2>&1; }
healthy() { [ "$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null || true)" = healthy ]; }
wait_healthy() { for _ in $(seq 120); do healthy "$1" && return 0; sleep 2; done; return 1; }
failed=0
need() { local description="$1"; shift; if "$@"; then echo "ok   $description"; else echo "FAIL $description"; failed=1; fi; }
file_exists() { [ -f "$1" ]; }
image_present() { docker image inspect "$1" >/dev/null 2>&1; }
env_true() { grep -qs '^M6B_ACTIVE=1' "$root/.env"; }
# The newest active layer decides which gateway and adapter images are expected (m6c.env over m6b.env).
layer_file() { if grep -qs '^M6C_ACTIVE=1' "$root/.env"; then echo "$root/m6c.env"; else echo "$root/m6b.env"; fi; }
layer_value() { grep -h "^$1=" "$(layer_file)" | cut -d= -f2-; }

native="$project-mcp-jira-1"
adapter="$project-mcp-jira-adapter-1"
foreign="$project-mcp-atlassian-1"
foreign_net="${project}_jira-foreign"
gateways=(mcp-gateway mcp-gateway-2)

connect_mock() {
  docker network inspect "$foreign_net" >/dev/null 2>&1 || fail "the network $foreign_net does not exist"
  docker network connect --alias jira.internal "$foreign_net" "$project-jira-mock-1" 2>/dev/null || true
}
restart_gateways() { # compose-function
  local fn="$1" gateway
  for gateway in "${gateways[@]}"; do
    "$fn" up -d --no-deps --wait "$gateway"
    wait_healthy "$project-$gateway-1" || wait_healthy "$project-$gateway" || fail "$gateway is not healthy; run 'scripts/adapter.sh off --confirm'"
    echo "adapter: $gateway healthy"
  done
}

case "${1:-status}" in
  status)
    for c in "$native" "$adapter" "$foreign"; do
      if present "$c"; then echo "adapter: $c $(docker inspect -f '{{.State.Status}}' "$c")"; else echo "adapter: $c absent"; fi
    done
    for g in "$project-mcp-gateway-1" "$project-mcp-gateway-2-1"; do
      echo "adapter: $g MCP_SERVER_JIRA_ADAPTER=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$g" | sed -n 's/^MCP_SERVER_JIRA_ADAPTER=//p' | head -1)"
    done
    ;;
  on)
    need "M6B_ACTIVE=1 (the gateways of 6.2.0 know adapter servers)" env_true
    need "the gateway runs an image of stage 3" test "$(docker inspect -f '{{.Config.Image}}' "$project-mcp-gateway-1")" = "$(layer_value MCP_GATEWAY_IMAGE)"
    need "both gateways healthy" bash -c "docker inspect -f '{{.State.Health.Status}}' $project-mcp-gateway-1 $project-mcp-gateway-2-1 | grep -vc '^healthy\$' | grep -q '^0\$'"
    need "the adapter image is present" image_present "$(layer_value ADAPTER_IMAGE)"
    need "the foreign server image is present (pinned by digest)" image_present "$(sed -n 's/^    image: \(ghcr.io[^ ]*\)$/\1/p' "$root/docker-compose.adapter.yml")"
    need "client certificate CN=mcp-adapter-mcp-jira exists" file_exists "$root/certs/mcp-adapter-mcp-jira/keystore.p12"
    need "the native mcp-jira exists (it is what gets replaced) or the adapter is already there" bash -c "docker inspect $native >/dev/null 2>&1 || docker inspect $adapter >/dev/null 2>&1"
    need "no bulk stand-in" bash -c "! docker inspect $project-mcp-bulk-1 >/dev/null 2>&1"
    [ "$failed" -eq 0 ] || exit 1
    if [ "${2:-}" != --confirm ]; then
      echo "plan: remove $native (its state volume stays); start adapter-state-init, $foreign and $adapter; connect the Jira mock to $foreign_net;"
      echo "      recreate mcp-gateway, then mcp-gateway-2 with MCP_SERVER_JIRA_ADAPTER=true, each after the health of the previous. Add --confirm to do it."
      exit 0
    fi
    trap 'echo "adapter: interrupted. Way back: scripts/adapter.sh off --confirm (idempotent)." >&2' ERR
    trap 'echo "adapter: stopped by a signal. Way back: scripts/adapter.sh off --confirm (idempotent)." >&2; exit 130' INT TERM
    if present "$native"; then compose rm -sf mcp-jira; fi
    overlay up -d --no-deps --wait adapter-state-init mcp-atlassian 2>&1 | tail -3
    connect_mock
    overlay up -d --no-deps --wait mcp-jira-adapter
    restart_gateways overlay
    for _ in $(seq 60); do
      source="$(psql_gateway "select source from registry_servers where server_id = 'mcp-jira'" || true)"
      [ "$source" = adapter ] && break
      sleep 2
    done
    echo "adapter: registry row of mcp-jira: source ${source:-none}"
    psql_gateway "select tool || ' ' || coalesce(pending_change, 'approved') from registry_tools where server_id = 'mcp-jira' order by tool" | sed 's/^/adapter: tool /'
    trap - ERR INT TERM
    ;;
  off)
    if [ "${2:-}" != --confirm ]; then
      echo "plan: gateways one at a time without adapter mode; remove $adapter and $foreign; disconnect the Jira mock; start the native mcp-jira;"
      echo "      register its manifest (make registry-register SERVERS=mcp-jira). Idempotent. Add --confirm to do it."
      exit 0
    fi
    trap 'echo "adapter: interrupted. Run scripts/adapter.sh off --confirm again; if the registry is the problem: make registry-register SERVERS=mcp-jira." >&2' ERR
    trap 'echo "adapter: stopped by a signal. Run scripts/adapter.sh off --confirm again." >&2; exit 130' INT TERM
    restart_gateways compose
    if present "$adapter" || present "$foreign"; then overlay rm -sf mcp-jira-adapter mcp-atlassian; fi
    docker network disconnect "$foreign_net" "$project-jira-mock-1" 2>/dev/null || true
    for n in "$foreign_net" "${project}_adapter-foreign"; do docker network rm "$n" >/dev/null 2>&1 || true; done
    compose up -d --no-deps --wait mcp-jira
    make -C "$root" --no-print-directory registry-register SERVERS="mcp-jira" | cut -c1-200
    trap - ERR INT TERM
    ;;
  *)
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
