#!/usr/bin/env bash
# Way back from a half done or rejected M5 switch to the M4 stack, with the backup that scripts/apply-m5-switch.sh made.
#
#   scripts/rollback-m5.sh --dry-run [out/m5-backup-<time>]    prints the steps, changes nothing
#   scripts/rollback-m5.sh --confirm [out/m5-backup-<time>]    does them (the backup of the last switch when none is given)
#   SKIP_DB=1 ...   leave the databases as they are (the switch failed before any M5 component ran its migrations)
#
# Steps: remove the M5 services (balancers, second instances: they hold ports 18000, 18445, 18100 and the TLS aliases) -> stop the M5
# gateway and broker -> restore the gateway and broker databases from the dumps (undoes V7, which dropped virtual_servers, and the
# broker migrations; everything written to these two databases since the backup is lost, Redis and OpenBao are not touched) ->
# remove M5_ACTIVE from .env -> wait for a 4.x policy bundle (a 4.x gateway and the servers refuse the 5.x one: ws-09 must publish the
# 4.x bundle again, sign "M5-ROLLBACK") -> recreate broker, MCP servers, gateway, BFF, web and proxy from the images of m4.env.
# Not undone: MCP sessions created by the M5 gateway in Redis (a 4.x gateway answers 404, clients initialize again), the policy
# state volume gateway-state-2 (unused after the rollback, removed with `docker volume rm` when wanted), the restarted mocks.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
mode="${1:-}"
case "$mode" in --dry-run|--confirm) shift ;; *) echo "usage: scripts/rollback-m5.sh --dry-run|--confirm [backup dir]" >&2; exit 2 ;; esac
state="$root/out/m5-switch.state"
backup="${1:-$(sed -n 's/^backup=//p' "$state" 2>/dev/null | tail -1)}"
case "$backup" in /*|"") ;; *) backup="$root/$backup" ;; esac
[ -n "$backup" ] && [ -d "$backup" ] || { echo "rollback-m5: no backup directory (give out/m5-backup-<time>)" >&2; exit 1; }
if [ "${SKIP_DB:-0}" != 1 ]; then
  for file in gateway.sql broker.sql; do
    [ -s "$backup/$file" ] || { echo "rollback-m5: $backup/$file is missing or empty" >&2; exit 1; }
  done
fi

run() {
  if [ "$mode" = "--dry-run" ]; then echo "  would run: $*"; else echo "rollback-m5: $*"; "$@"; fi
}
# The M5 services must be addressable for the removal, so the layer is on for those commands and off for the M4 ones.
m5() { M5_ACTIVE=1 "$root/scripts/compose.sh" --profile full --profile m2 "$@"; }
m4() { M5_ACTIVE=0 "$root/scripts/compose.sh" --profile full --profile m2 "$@"; }
pg_exec() { m4 exec -T postgres "$@"; }
# shellcheck source=m5-lib.sh
. "$root/scripts/m5-lib.sh"

echo "rollback-m5: backup $backup ($mode)"
run m5 rm -sf gateway-lb broker-lb mcp-gateway-2 mcp-credential-broker-2
run m5 stop mcp-gateway mcp-credential-broker
if [ "${SKIP_DB:-0}" != 1 ]; then
  if [ "$mode" = "--dry-run" ]; then echo "  would run: restore gateway and broker databases from $backup"; else m5_restore_databases "$backup"; fi
fi
if [ "$mode" = "--dry-run" ]; then
  echo "  would run: remove M5_ACTIVE from .env"
else
  sed -i.bak '/^M5_ACTIVE=/d' "$root/.env" && rm -f "$root/.env.bak"
fi

bundle_ok() { # a 4.x main bundle is the latest one
  python3 -c '
import sys; sys.path.insert(0, sys.argv[1])
import m5
sys.exit(0 if m5.bundle_schema_version(m5.latest()).startswith("4.") else 1)' "$root/scripts" && return 0 || return 1
}
if [ "$mode" = "--dry-run" ]; then
  echo "  would run: wait for a 4.x main bundle (SIGNAL to ws-09), unless the latest one already is 4.x"
elif ! bundle_ok; then
  latest="$(python3 "$root/scripts/m5.py" bundle-latest)"
  echo "rollback-m5: SIGNAL to ws-09: the stack returns to 4.x components; publish the 4.x bundle again (latest revision now: $latest)"
  python3 "$root/scripts/m5.py" bundle-wait "$latest" 900 4. || { echo "rollback-m5: no 4.x bundle within 15 minutes; publish it and run the rollback again with SKIP_DB=1" >&2; exit 1; }
fi

run m4 run --rm --no-deps openbao-init
run m4 up -d --no-deps --wait mcp-credential-broker
run m4 up -d --no-deps --wait server-state-init mcp-jira mcp-confluence mcp-gitlab mcp-servicedesk mcp-opensearch
run m4 up -d --no-deps --wait mcp-gateway
run m4 up -d --no-deps --wait portal-bff portal-web
run m4 up -d --no-deps --wait proxy
if [ "$mode" = "--confirm" ]; then
  sed -i.bak 's/^phase=.*/phase=rolled-back/' "$state" 2>/dev/null && rm -f "$state.bak" || true
  echo "rollback-m5: done; check with make m4 and the proxy tests (PROXY_LAYER=m4 scripts/proxy-test.sh)"
fi
