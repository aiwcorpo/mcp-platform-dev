#!/usr/bin/env bash
# One-off switch of the RUNNING stack from the in-memory dev OpenBao (-dev, no volume) to the persistent one (raft on the
# volume openbao-data, openbao-unseal sidecar). Dev stack only. Touches OpenBao and nothing else: no other container is stopped,
# restarted or recreated, no make clean/reset/down.
#
#   scripts/openbao-migrate.sh --check      preconditions and plan, changes nothing (`make openbao-migrate CHECK=1`)
#   scripts/openbao-migrate.sh --confirm    the switch (`make openbao-migrate CONFIRM=1`)
#
# WHAT IS LOST: everything the in-memory OpenBao holds. The new instance starts empty and openbao-init recreates only what it
# can derive from .secrets/dev.env (Transit keys, policy, AppRole, GitLab and OpenSearch clients, the Service Desk service
# account). Gone for good: saved connections of all users (PAT, OAuth), Authenticator registrations (TOTP seeds, encrypted under
# the old Transit key), anything written by hand. Users reconnect their systems and register their Authenticators again.
# The broker and the gateway need no restart: the broker logs in lazily with AppRole, and a 403 from the old token makes it log
# in again; while OpenBao is down or sealed it fails closed (503).
#
# Idempotent: when OpenBao already runs from the volume the script says so and does nothing.
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
cid="$("${c[@]}" ps -q openbao 2>/dev/null || true)"
if [ -n "$cid" ]; then
  mounts="$(docker inspect --format '{{range .Mounts}}{{.Destination}} {{end}}' "$cid")"
  case " $mounts" in
    *" /openbao/file "*) echo "openbao-migrate: OpenBao already runs from the volume openbao-data, nothing to do"; exit 0 ;;
  esac
  docker inspect --format '{{join .Config.Cmd " "}}' "$cid" | grep -q -- '-dev' \
    || fail "the running OpenBao is neither in -dev mode nor on the volume: not touching it"
else
  echo "openbao-migrate: no running OpenBao container; the new one will simply be started"
fi
grep -q '^BAO_ROOT_TOKEN=' "$root/.secrets/dev.env" || fail "BAO_ROOT_TOKEN missing in .secrets/dev.env (make secrets)"

cat <<'TXT'
openbao-migrate: PLAN
  1. scripts/init-secrets.sh                      creates .secrets/openbao (700), nothing else changes
  2. up -d --no-deps --force-recreate openbao     the in-memory instance is replaced: ALL ITS SECRETS ARE LOST
  3. up -d --no-deps --wait openbao-unseal        init + unseal, key and root token into .secrets/openbao (600)
  4. run --rm --no-deps openbao-init              Transit keys, policy, AppRole (new 168 h secret_id), clients, service account
  Lost: saved connections (PAT, OAuth) of all users, Authenticator registrations. Not touched: Keycloak, Postgres, Redis, mocks, broker, gateway.
TXT
if [ "$mode" = "--check" ]; then echo "openbao-migrate: --check done, nothing changed"; exit 0; fi

"$root/scripts/init-secrets.sh" >/dev/null
echo "openbao-migrate: replacing OpenBao"
"${c[@]}" up -d --no-deps --force-recreate openbao
"${c[@]}" up -d --no-deps --wait openbao-unseal
"${c[@]}" --profile full --profile m2 run --rm --no-deps openbao-init

for f in unseal-key root-token; do
  [ "$(stat -f %Lp "$root/.secrets/openbao/$f" 2>/dev/null || stat -c %a "$root/.secrets/openbao/$f")" = 600 ] || fail "$f is missing or not mode 600"
done
"${c[@]}" exec -T openbao bao status >/dev/null || fail "OpenBao is not unsealed"
echo "openbao-migrate: done. OpenBao is persistent (volume openbao-data), unsealed, keys in .secrets/openbao."
echo "openbao-migrate: users must reconnect their systems and register their Authenticators again (docs/m5.md)."
