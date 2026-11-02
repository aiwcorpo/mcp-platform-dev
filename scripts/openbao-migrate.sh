#!/usr/bin/env bash
# One-off switch of the RUNNING stack from the in-memory dev OpenBao (-dev, no volume) to the persistent one (raft on the
# volume openbao-data, openbao-unseal sidecar). Dev stack only. Touches OpenBao and nothing else: no other container is stopped,
# restarted or recreated, no make clean/reset/down.
#
#   scripts/openbao-migrate.sh --check      preconditions and plan, changes nothing (`make openbao-migrate CHECK=1`)
#   scripts/openbao-migrate.sh --confirm    the switch (`make openbao-migrate CONFIRM=1`)
#
# WHAT IS LOST: everything the in-memory OpenBao holds, and it cannot be carried over: the Transit keys were created
# non-exportable (`transit/export` answers "private key material is not exportable", checked on a throw-away instance), so the
# stored ciphertext is useless in any new instance. The new instance starts empty and openbao-init recreates only what it can
# derive from .secrets/dev.env (Transit keys, policy, AppRole, GitLab and OpenSearch clients, the Service Desk service account).
# Gone for good: saved connections of all users (PAT, OAuth), Authenticator registrations (TOTP seeds), anything written by hand.
# --check prints how many rows of the broker database depend on it (counts only).
#
# AFTER THE SWITCH the rows in PostgreSQL stay (this script deletes none, accounts alice, bob, carol, mallory included), so the
# portal still shows the connections as "connected" and the Authenticators as "active" although their secrets are gone, and a new
# registration answers 409 already_registered. Per user: remove the connection in the portal (or DELETE it), then connect again;
# for the Authenticator first `make reset-authenticator USER=<login>` (shared accounts alice, bob, carol, mallory need FORCE=1 and
# belong to people), then register. `m5.py connections compare` counts database rows, so it stays green; it now also reports rows
# without a secret in OpenBao.
#
# The broker and the gateway need no restart: the broker logs in lazily with AppRole, and a 403 from the old token makes it log
# in again; while OpenBao is down or sealed it fails closed (503).
#
# Idempotent and repeatable: when OpenBao already runs from the volume nothing is replaced, but openbao-init and the final checks
# run again, so a switch that failed half way is finished by running it once more.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
c=("$root/scripts/compose.sh")
mode="${1:-}"
case "$mode" in
  --check|--confirm) ;;
  *) echo "usage: scripts/openbao-migrate.sh --check | --confirm   (the switch needs --confirm; see the header)" >&2; exit 2 ;;
esac

fail() { echo "openbao-migrate: $*" >&2; exit 1; }

"${c[@]}" --profile full --profile m2 config -q || fail "compose configuration does not render"
already=0
cid="$("${c[@]}" ps -q openbao 2>/dev/null || true)"
if [ -n "$cid" ]; then
  mounts="$(docker inspect --format '{{range .Mounts}}{{.Destination}} {{end}}' "$cid")"
  case " $mounts" in
    *" /openbao/file "*) already=1 ;;
  esac
  if [ "$already" = 0 ]; then
    docker inspect --format '{{join .Config.Cmd " "}}' "$cid" | grep -q -- '-dev' \
      || fail "the running OpenBao is neither in -dev mode nor on the volume: not touching it"
  fi
else
  echo "openbao-migrate: no running OpenBao container; the new one will simply be started"
fi
grep -q '^BAO_ROOT_TOKEN=' "$root/.secrets/dev.env" || fail "BAO_ROOT_TOKEN missing in .secrets/dev.env (make secrets)"

db_counts() {
  "${c[@]}" --profile full --profile m2 exec -T postgres psql -U broker -d broker -At -F ' ' -v ON_ERROR_STOP=1 \
    -c "select 'connections', state, type, count(*) from connection group by 2,3 order by 2,3" \
    -c "select 'authenticators', state, '-', count(*) from authenticator group by 2 order by 2" 2>/dev/null \
    | sed 's/^/    /' || echo "    (broker database not readable)"
}
if [ "$already" = 1 ]; then
  echo "openbao-migrate: OpenBao already runs from the volume openbao-data; only openbao-init and the checks will run"
else
  echo "openbao-migrate: rows in the broker database that lose their secret (counts only):"
  db_counts
  cat <<'TXT'
openbao-migrate: PLAN
  1. scripts/init-secrets.sh                      creates .secrets/openbao (700), nothing else changes
  2. up -d --no-deps --force-recreate openbao     the in-memory instance is replaced: ALL ITS SECRETS ARE LOST (no way to save them: Transit keys are not exportable)
  3. up -d --no-deps --wait openbao-unseal        init + unseal, key and root token into .secrets/openbao (600)
  4. run --rm --no-deps openbao-init              Transit keys, policy, AppRole (new secret_id), clients, service account
  Lost: saved connections (PAT, OAuth) of all users, Authenticator registrations; the database rows stay and say "connected"/"active" (see the header).
  Not touched: Keycloak, Postgres, Redis, mocks, broker, gateway.
  Way back: none for the data. Undoing the switch means the previous compose definition and an empty -dev OpenBao (docs/m5.md).
TXT
fi
if [ "$mode" = "--check" ]; then echo "openbao-migrate: --check done, nothing changed"; exit 0; fi

export OPENBAO_MIGRATE_CONFIRMED=1
"$root/scripts/init-secrets.sh" >/dev/null
if [ "$already" = 0 ]; then
  echo "openbao-migrate: replacing OpenBao"
  "${c[@]}" up -d --no-deps --force-recreate openbao
fi
"${c[@]}" up -d --no-deps --wait openbao-unseal
"${c[@]}" --profile full --profile m2 run --rm --no-deps openbao-init

for f in unseal-key root-token; do
  [ "$(stat -f %Lp "$root/.secrets/openbao/$f" 2>/dev/null || stat -c %a "$root/.secrets/openbao/$f")" = 600 ] || fail "$f is missing or not mode 600"
done
"${c[@]}" exec -T openbao bao status >/dev/null || fail "OpenBao is not unsealed"
echo "openbao-migrate: done. OpenBao is persistent (volume openbao-data), unsealed, keys in .secrets/openbao."
echo "openbao-migrate: the portal still shows the old connections as connected and the Authenticators as active, but their secrets are gone."
echo "openbao-migrate: per user remove the connection and connect again; Authenticator: make reset-authenticator USER=<login> [FORCE=1 for alice, bob, carol, mallory], then register (docs/m5.md)."
