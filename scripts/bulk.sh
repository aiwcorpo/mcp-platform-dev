#!/usr/bin/env bash
# Profile bulk on the main stack: for the time of a test (scenario G9 of mcp-e2e, tools/list over 100 tools) the real mcp-confluence is
# replaced by a stand-in with 250 READ_ONLY tools (plus the four real ones), then put back. The contracts know five systems and the gateway refuses another, so
# the stand-in takes the identity of mcp-confluence (docker-compose.bulk.yml). The change is visible to every user of the stack while it
# lasts: the four real Confluence tools stay in the list (with the manifest of the real build, so their approvals in the registry survive)
# but answer with an error. The state volume of the real server is kept.
#
#   scripts/bulk.sh status           what runs now (nothing is changed)
#   scripts/bulk.sh on  [--confirm]  without --confirm only the checks and the plan; with it: register the manifest of the stand-in
#                                    (CN mcp-registry-ci-mcp-confluence), remove the real server, start the stand-in, wait for 253 tools
#   scripts/bulk.sh off [--confirm]  remove the stand-in, start the real server, register its manifest again (the build artifact of
#                                    ../mcp-server-confluence), wait until the gateway lists the real tools
# Only on the order of the coordinator (it recreates nothing but the two confluence containers, and restarts no gateway). While the
# stand-in exists, `make up*` (scripts/bulk-guard.sh) and `compose up` (scripts/compose.sh) are refused: the real server would start next to it
# under the same address. Both `on` and `off` are idempotent; the manifest is registered before anything is removed.
# Interrupted: run `off --confirm` again. Neither server running: `make up-m2` (or `off --confirm`). Registry left with the 254-tool manifest
# while the real server runs: `make registry-register SERVERS=mcp-confluence`.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
project="$(grep -s '^COMPOSE_PROJECT_NAME=' "$root/.env" | cut -d= -f2- || true)"
project="${project:-mcp-dev}"
compose() { "$root/scripts/compose.sh" "$@"; }
overlay() { BULK_SWITCH=1 "$root/scripts/compose.sh" -f "$root/docker-compose.bulk.yml" --profile bulk "$@"; }
manifest_of_real="$root/../mcp-server-confluence/mcp-app/target/mcp-manifest.json"
gateway_url() { echo "http://localhost:$(grep -s '^GATEWAY_PORT=' "$root/.env" | cut -d= -f2-)/mcp"; }

running() { [ "$(docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || true)" = running ]; }
healthy() { [ "$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null || true)" = healthy ]; }
present() { docker inspect "$1" >/dev/null 2>&1; }

real="$project-mcp-confluence-1"
standin="$project-mcp-bulk-1"
failed=0
need() { # description command...
  local description="$1"
  shift
  if "$@"; then echo "ok   $description"; else echo "FAIL $description"; failed=1; fi
}
file_exists() { [ -f "$1" ]; }
standin_image_present() { docker image inspect "$1" >/dev/null 2>&1; }

checks_common() {
  local gateway="mcp-gateway-1"
  present "$project-gateway-lb-1" && gateway="gateway-lb-1"
  need "the gateway ($gateway) is healthy" healthy "$project-$gateway"
  need "certificate of the stand-in exists (make certs)" file_exists "$root/certs/mcp-bulk/tls.crt"
  need "the python image of the mocks is present" standin_image_present python:3.12-alpine@sha256:0687a6bc9716edc2a6ee0fbfb0f87e7ee358b262b67c9215de91bc9b2d38ba71
  need "the manifest of the real server exists ($manifest_of_real), so that off can restore it" file_exists "$manifest_of_real"
  need "registry certificate of mcp-confluence exists" file_exists "$root/certs/mcp-registry-ci-mcp-confluence/tls.crt"
}

wait_for() { # url expect
  python3 -I "$root/scripts/bulk-check.py" --url "$1" --expect "$2" --wait 90
}

case "${1:-status}" in
  status)
    if present "$standin"; then echo "bulk: the stand-in exists ($(docker inspect -f '{{.State.Status}}' "$standin"))"; else echo "bulk: no stand-in"; fi
    if present "$real"; then echo "bulk: the real mcp-confluence exists ($(docker inspect -f '{{.State.Status}}' "$real"))"; else echo "bulk: no real mcp-confluence"; fi
    ;;
  on)
    checks_common
    [ "$failed" -eq 0 ] || exit 1
    if present "$standin"; then
      echo "bulk: the stand-in exists; registering its manifest again and checking the list (nothing else changes)"
    else
      need "the real mcp-confluence exists (it is what gets replaced)" present "$real"
      [ "$failed" -eq 0 ] || exit 1
    fi
    if [ "${2:-}" != --confirm ]; then
      echo "plan: PUT the manifest of the stand-in as mcp-confluence FIRST (the registry check; nothing is removed if it fails), then"
      echo "      remove $real (volume mcp-confluence-state stays), start $standin under the address mcp-confluence.internal,"
      echo "      wait until $(gateway_url) lists 253 tools to a reader (250 of the stand-in, 3 real). Add --confirm to do it."
      exit 0
    fi
    trap 'echo "bulk: interrupted. Way back: scripts/bulk.sh off --confirm (idempotent). If nothing of mcp-confluence runs: make up-m2." >&2' ERR
    trap 'echo "bulk: stopped by a signal. Way back: scripts/bulk.sh off --confirm (idempotent). If nothing of mcp-confluence runs: make up-m2." >&2; exit 130' INT TERM
    manifest="$(mktemp)"
    python3 -I "$root/scripts/stub-manifest.py" confluence > "$manifest"
    registered="$(python3 -I "$root/scripts/mcpdev.py" registry-put mcp-confluence "$manifest")" || true
    rm -f "$manifest"
    echo "$registered" | cut -c1-300
    grep -q '"httpStatus": 200' <<<"$registered" || { echo "bulk: the registry refused the manifest (reason above); nothing was changed" >&2; trap - ERR INT TERM; exit 1; }
    if ! present "$standin"; then
      compose --profile m2 rm -sf mcp-confluence
      overlay up -d --no-deps --wait mcp-bulk
    fi
    wait_for "$(gateway_url)" bulk
    trap - ERR INT TERM
    ;;
  off)
    need "the manifest of the real server exists ($manifest_of_real)" file_exists "$manifest_of_real"
    [ "$failed" -eq 0 ] || exit 1
    if [ "${2:-}" != --confirm ]; then
      echo "plan: remove $standin if it exists, make sure the real mcp-confluence runs (image of .env, state volume kept), register its manifest,"
      echo "      wait until $(gateway_url) lists the real Confluence tools. Idempotent. Add --confirm to do it."
      exit 0
    fi
    trap 'echo "bulk: interrupted. Run scripts/bulk.sh off --confirm again; if the registry is the problem: make registry-register SERVERS=mcp-confluence." >&2' ERR
    trap 'echo "bulk: stopped by a signal. Run scripts/bulk.sh off --confirm again." >&2; exit 130' INT TERM
    if present "$standin"; then overlay rm -sf mcp-bulk; fi
    compose --profile full --profile m2 up -d --no-deps --wait mcp-confluence
    python3 -I "$root/scripts/mcpdev.py" registry-put mcp-confluence "$manifest_of_real" | cut -c1-300
    wait_for "$(gateway_url)" real
    trap - ERR INT TERM
    ;;
  *)
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
