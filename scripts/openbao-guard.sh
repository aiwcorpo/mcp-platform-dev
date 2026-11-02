#!/usr/bin/env bash
# Refuses (exit 3) a `compose up` that would recreate a running in-memory (-dev) OpenBao: that instance holds every user secret and
# the persistent definition replaces it, so the secrets would be gone without anyone asking. Used by scripts/compose.sh for every
# `up`, and by the Makefile before `up`, `up-core`, `up-full` and `up-m2` (their first step removes containers). The one-off
# switch is scripts/openbao-migrate.sh, which sets OPENBAO_MIGRATE_CONFIRMED=1 for its own calls.
#   scripts/openbao-guard.sh [compose arguments of the intended command]   default: --profile infra --profile full --profile m2 up -d
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
[ "${OPENBAO_MIGRATE_CONFIRMED:-0}" != 1 ] || exit 0
args=("$@")
[ "${#args[@]}" -gt 0 ] || args=(--profile infra --profile full --profile m2 up -d)
inner() { OPENBAO_MIGRATE_CONFIRMED=1 "$root/scripts/compose.sh" "$@"; }

cid="$(inner ps -q openbao 2>/dev/null | head -1 || true)"
[ -n "$cid" ] || exit 0
docker inspect --format '{{join .Config.Cmd " "}} ' "$cid" 2>/dev/null | grep -q -- '-dev ' || exit 0
! docker inspect --format '{{range .Mounts}}{{.Destination}} {{end}}' "$cid" | grep -q '/openbao/file' || exit 0
plan="$(inner "${args[@]}" --dry-run 2>&1 || true)"
if grep -Eq 'Container [^ ]*openbao-1 +(Recreate|Stopping|Removing)' <<<"$plan"; then
  echo "openbao-guard: REFUSED. The running OpenBao is in -dev mode (memory only); this command would recreate it and delete all its secrets" >&2
  echo "openbao-guard: (saved connections, Authenticator registrations). One-off switch: make openbao-migrate CHECK=1, then CONFIRM=1 (docs/m5.md)." >&2
  exit 3
fi
