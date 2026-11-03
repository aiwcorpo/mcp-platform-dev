#!/usr/bin/env bash
# Moves the stack to the images of m6b.env (M6 stage 3, contracts 6.2.0), one instance at a time, each followed by a health check:
#   broker-1, broker-2, gateway-1, gateway-2, (the gateway balancer recreated), mcp-jira, mcp-confluence, mcp-gitlab, mcp-opensearch, portal-bff, portal-web.
# Nothing else is recreated (OpenBao, PostgreSQL, Keycloak, Redis, mocks, mcp-servicedesk, the broker balancer and the proxy stay as they are);
# `up --no-deps` names the service. The gateway balancer is recreated once, after both gateways (all traffic through it breaks for a few seconds:
# open streams, JWKS and the admin API on :8443 included). The init jobs are never run. New tool definitions of the servers arrive in the registry as pending and are listed at the end;
# approving UPDATE and DESTRUCTIVE tools is a decision of an administrator, not of this script.
#   scripts/apply-m6b-switch.sh check              the plan of the project (what Compose would recreate) and the counts, changes nothing
#   scripts/apply-m6b-switch.sh apply --confirm    M6B_ACTIVE=1 in .env, then the services in turn
#   scripts/apply-m6b-switch.sh rollback-check    the conditions of a rollback, as code (nothing is changed)
#   scripts/apply-m6b-switch.sh rollback --confirm M6B_ACTIVE=0: gateway and broker back to m6.env, servers and portal to m5.env, then the registry rows
#                                                  of the four servers are dropped and the gateways restarted one at a time, so that the m5 servers are
#                                                  taken over again by bootstrap (the manifests of m5 exist only in the running m5 servers)
#   scripts/apply-m6b-switch.sh pending            the tools waiting for approval (SELECT only)
# Rollback is refused (rollback-check) while: a kill switch is active (the old gateway ignores them; FORCE=1 overrides), the adapter exists or a gateway
# is in adapter mode (the old gateway does not know adapter servers), the published policy bundle has another Cedar schema than the old gateway and
# the m5 servers carry (they would refuse it and keep the previous bundle), or the stack is not healthy. V8 and V9 stay in the database; the old image
# ignores them. Dropping the registry rows loses the approvals of the new UPDATE and DESTRUCTIVE tools and of the old ones: after a rollback the
# bootstrap approves what the m5 servers present, as it did before M6.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
project="$(grep -s '^COMPOSE_PROJECT_NAME=' "$root/.env" | cut -d= -f2- || true)"
project="${project:-mcp-dev}"
unset M6_ACTIVE M6B_ACTIVE   # a value exported in the shell would win over .env in scripts/compose.sh
mode="${1:-check}"
if grep -qs '^M6C_ACTIVE=1' "$root/.env" && [ "$mode" != pending ]; then
  echo "apply-m6b-switch: M6C_ACTIVE=1 (m6c.env, migration V10 of the gateway): this script's apply and rollback no longer describe the stack; only 'pending' works" >&2
  exit 1
fi
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

# The balancer reads lb/gateway.conf through a bind mount; an in-place edit of the file on the host is not seen whole by the running container
# (nginx -t found it cut off), so the balancer is recreated: it is stateless and starts in seconds, with the 502 and 504 pages of BalancerError.
recreate_balancer() {
  compose up -d --force-recreate --no-deps --wait gateway-lb
  wait_healthy "$(container gateway-lb)" || fail "gateway-lb is not healthy after the recreate"
  echo "apply-m6b-switch: gateway-lb recreated (502 and 504 pages of BalancerError, read timeout 120 s)"
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
    if [ "$service" = mcp-gateway-2 ]; then recreate_balancer; fi
  done
  echo "apply-m6b-switch: all services done"
}

# Cedar schema (sha256) that an image carries in its contracts library, read from the jar in the image without starting it.
image_schema() {
  local image="$1" id tmp
  tmp="$(mktemp -d)"
  id="$(docker create "$image")"
  docker cp "$id:/app/app.jar" "$tmp/app.jar" >/dev/null
  docker rm "$id" >/dev/null
  python3 -I - "$tmp/app.jar" <<'PY'
import hashlib, io, sys, zipfile
outer = zipfile.ZipFile(sys.argv[1])
for name in outer.namelist():
    if name.endswith(".jar") and "mcp-contracts" in name:
        inner = zipfile.ZipFile(io.BytesIO(outer.read(name)))
        for entry in inner.namelist():
            if entry.endswith("cedar/mcp.cedarschema"):
                print(hashlib.sha256(inner.read(entry)).hexdigest())
                sys.exit(0)
sys.exit(1)
PY
  rm -rf "$tmp"
}
bundle_schema() { # the schema of the published main bundle
  local repo
  repo="$(grep -s '^POLICY_REPOSITORY_DIR=' "$root/.env" | cut -d= -f2-)"
  case "$repo" in /*) ;; *) repo="$root/$repo" ;; esac
  python3 -I - "$repo/main" <<'PY'
import hashlib, json, sys, tarfile
latest = json.load(open(sys.argv[1] + "/latest.json"))
with tarfile.open(sys.argv[1] + "/" + latest["url"]) as archive:
    print(hashlib.sha256(archive.extractfile("schema.cedarschema").read()).hexdigest())
PY
}
old_images() { # gateway of m6.env and the servers of m5.env, the images a rollback returns to
  grep -h '^MCP_GATEWAY_IMAGE=' "$root/m6.env" | cut -d= -f2-
  grep -hE '^MCP_(JIRA|CONFLUENCE|GITLAB|OPENSEARCH)_IMAGE=' "$root/m5.env" | cut -d= -f2-
}
rollback_conditions() { # prints one line per condition; returns non-zero when one fails
  local bad=0 active image want have gateway
  active="$(psql_in gateway "select count(*) from kill_switches" 2>/dev/null || echo unknown)"
  if [ "$active" = 0 ] || [ "${FORCE:-0}" = 1 ]; then echo "ok   kill switches: $active active"; else echo "FAIL kill switches active or unreadable ($active): the old gateway ignores them (FORCE=1 overrides)"; bad=1; fi
  if docker inspect "$project-mcp-jira-adapter-1" >/dev/null 2>&1; then echo "FAIL the adapter of profile adapter exists: scripts/adapter.sh off --confirm first (the old gateway does not know adapter servers)"; bad=1
  else echo "ok   no adapter container"; fi
  for gateway in mcp-gateway-1 mcp-gateway-2-1; do
    if docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$project-$gateway" | grep -q '^MCP_SERVER_JIRA_ADAPTER=true'; then
      echo "FAIL $gateway is in adapter mode for mcp-jira"; bad=1
    fi
  done
  want="$(bundle_schema)" || { echo "FAIL cannot read the published policy bundle"; return 1; }
  while read -r image; do
    have="$(image_schema "$image")" || have=unreadable
    if [ "$have" = "$want" ]; then echo "ok   $image carries the Cedar schema of the published bundle"; else echo "FAIL $image carries another Cedar schema than the published bundle ($have, bundle $want): it would refuse the bundle and keep the previous one"; bad=1; fi
  done < <(old_images)
  [ -z "$(unhealthy)" ] && echo "ok   everything is healthy" || { echo "FAIL not healthy: $(unhealthy)"; bad=1; }
  return "$bad"
}
# After a rollback: the m5 servers run again and the registry still holds the manifests registered for the m6 builds, so their tools would be blocked as
# changed. The rows of the four servers are dropped (their approvals go with them) and the gateways restarted one at a time: the bootstrap of the dev
# gateway takes the manifests over from the running servers, which is the only place the m5 manifests exist.
reset_registry() {
  local servers="'mcp-jira','mcp-confluence','mcp-gitlab','mcp-opensearch'" count gateway
  psql_in gateway "delete from registry_tools where server_id in ($servers)" >/dev/null
  psql_in gateway "delete from registry_servers where server_id in ($servers)" >/dev/null
  echo "apply-m6b-switch: registry rows of the four servers dropped"
  for gateway in mcp-gateway mcp-gateway-2; do
    compose up -d --force-recreate --no-deps --wait "$gateway"
    wait_healthy "$(container "$gateway")" || fail "$gateway is not healthy after the restart"
  done
  for _ in $(seq 60); do
    count="$(psql_in gateway "select count(*) from registry_servers where server_id in ($servers) and source = 'bootstrap'")"
    [ "${count:-0}" -ge 4 ] && break
    sleep 2
  done
  [ "${count:-0}" -ge 4 ] || fail "only ${count:-0} of 4 manifests were taken over again; check the gateway log"
  echo "apply-m6b-switch: manifests of the m5 servers taken over again: $(psql_in gateway "select sum(jsonb_array_length(manifest->'tools')) from registry_servers where server_id in ($servers)") tools, pending: $(psql_in gateway "select count(*) from registry_tools where pending_entry is not null")"
}

case "$mode" in
  check)
    echo "now: $(counts)"
    echo "plan of the whole project with M6B_ACTIVE=1 (init jobs are listed too; this script never runs them):"
    # The dry run ends by waiting for the health of simulated containers, so it is cut after 60 s (python, as macOS has no timeout(1)).
    M6B_ACTIVE=1 python3 -I -c '
import subprocess, sys
try:
    out = subprocess.run(sys.argv[1:], capture_output=True, text=True, timeout=60)
    print(out.stdout + out.stderr)
except subprocess.TimeoutExpired as e:
    print((e.stdout or b"").decode() + (e.stderr or b"").decode())' "$root/scripts/compose.sh" --profile full --profile m2 up -d --dry-run 2>&1 | grep -E 'Recreate$' | sort -u || true
    for service in "${order[@]}"; do echo "image of $service now: $(docker inspect -f '{{.Config.Image}}' "$(container "$service")")"; done
    ;;
  apply)
    [ "${2:-}" = --confirm ] || fail "add --confirm"
    grep -q '^M6_ACTIVE=1' "$root/.env" || fail "M6_ACTIVE=1 is not set in .env (scripts/apply-m6-switch.sh first); .env is left as it is"
    [ -z "$(unhealthy)" ] || fail "not everything is healthy before the switch: $(unhealthy)"
    echo "before: $(counts)"
    set_flag 1
    roll
    echo "after:  $(counts)"
    ;;
  rollback-check)
    rollback_conditions
    ;;
  rollback)
    [ "${2:-}" = --confirm ] || fail "add --confirm"
    rollback_conditions || fail "the conditions of a rollback are not met (see above)"
    echo "before: $(counts)"
    set_flag 0
    roll
    reset_registry
    echo "after:  $(counts)"
    ;;
  pending)
    pending
    ;;
  *) grep -E '^#   scripts/' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
