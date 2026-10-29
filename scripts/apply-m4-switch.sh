#!/usr/bin/env bash
# Applies the M4 switch (mcp-contracts 4.0.0) to the RUNNING stack without down/clean/reset: the components are recreated from
# the images in .env, in dependency order; PostgreSQL, Redis (AOF volume), OpenBao data and the Authenticator registrations stay.
#
#   scripts/apply-m4-switch.sh --check    only the preconditions below, changes nothing (also what `make m4-switch CHECK=1` runs)
#   scripts/apply-m4-switch.sh            the switch
#
# Preconditions (the script stops before touching anything when one is missing):
#   - the images of .env exist locally: broker, gateway, five MCP servers, BFF, web;
#   - the dev- public keys of ws-09 exist in mcp-policies/keys/dev (dev-policies-main, dev-policies-overrides); the script sets
#     POLICY_KEY_MAIN_ID and POLICY_KEY_OVERRIDES_ID in .env;
#   - a manifest built from each server repository (target/mcp-manifest.json or MANIFEST_DIR/<serverId>.json): the registry
#     entries are replaced through the CI certificates (CC-161), not through the bootstrap of the gateway.
# Order: OpenBao init (service account of Service Desk, broker policy) -> Redis ACL -> broker -> MCP servers (they declare
# environment dev) -> drop of the bootstrap rows of the registry (dev only; the CI PUT recreates them with source ci) ->
# gateway -> registration by CI -> BFF and web -> signal to ws-09 and wait until the 4.0.0 bundle signed with the dev- key is
# published (ws-09 runs .ci/switch-stack.sh --confirm in mcp-policies; M4_PUBLISHED=1 skips the wait) -> proxy-test and make m4.
# The components start before the publication: until it, they do not load a bundle (the published one is 3.1.x with test- keys).
# Rollback is not automatic: the images that ran before are written to out/m4-previous-images.txt, but the gateway keeps the
# accepted policy revision (rollback protection) and the registry rows are those of the CI PUT.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
compose="$root/scripts/compose.sh"
c=("$compose" --profile full --profile m2)
check_only=0
[ "${1:-}" = "--check" ] && check_only=1

fail() { echo "apply-m4-switch: $*" >&2; exit 1; }

config="$("${c[@]}" config --format json)"
images="$(python3 -c '
import json, sys
for name, svc in sorted(json.load(sys.stdin)["services"].items()):
    if name in ("mcp-gateway", "mcp-credential-broker", "mcp-jira", "mcp-confluence", "mcp-gitlab", "mcp-servicedesk",
                "mcp-opensearch", "portal-bff", "portal-web"):
        print(name, svc["image"])' <<<"$config")"
while read -r service image; do
  docker image inspect "$image" >/dev/null 2>&1 || fail "$service: image $image does not exist locally (make images / make images-m2)"
  case "$image" in
    *:m3-*|*:m2-*|*:m1-*) fail "$service: image $image is an M1-M3 image; set the M4 image in .env" ;;
  esac
done <<<"$images"
echo "apply-m4-switch: images ok"

for key in dev-policies-main dev-policies-overrides; do
  [ -f "$root/../mcp-policies/keys/dev/$key.public.pem" ] || fail "missing ../mcp-policies/keys/dev/$key.public.pem (ws-09)"
done
for server in mcp-jira mcp-confluence mcp-gitlab mcp-servicedesk mcp-opensearch; do
  manifest="${MANIFEST_DIR:+$MANIFEST_DIR/$server.json}"
  manifest="${manifest:-$root/../mcp-server-${server#mcp-}/target/mcp-manifest.json}"
  [ -f "$manifest" ] || fail "$server: no manifest at $manifest (build the server; the registry is filled by the CI PUT)"
done
echo "apply-m4-switch: dev keys and manifests ok"
[ "$check_only" = 1 ] && { echo "apply-m4-switch: --check passed, nothing changed"; exit 0; }

mkdir -p "$root/out"
docker ps --format '{{.Names}} {{.Image}}' | grep '^mcp-dev-' > "$root/out/m4-previous-images.txt" || true

python3 - "$root/.env" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
for key, value in (("POLICY_KEY_MAIN_ID", "dev-policies-main"), ("POLICY_KEY_OVERRIDES_ID", "dev-policies-overrides")):
    text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
open(path, "w").write(text)
PY
echo "apply-m4-switch: policy keys in .env point at the dev- keys"

"${c[@]}" run --rm --no-deps openbao-init
"$root/scripts/redis-sync-acl.sh" >/dev/null
"${c[@]}" up -d --no-deps --wait mcp-credential-broker
"${c[@]}" up -d --no-deps --wait server-state-init mcp-jira mcp-confluence mcp-gitlab mcp-servicedesk mcp-opensearch
"${c[@]}" exec -T postgres psql -U gateway -d gateway -At -c "delete from registry_servers where source = 'bootstrap'" \
  | sed 's/^/apply-m4-switch: dropped bootstrap rows: /'
"${c[@]}" up -d --no-deps --wait mcp-gateway
"$root/scripts/registry-register.sh"
"${c[@]}" up -d --no-deps --wait portal-bff portal-web
"${c[@]}" restart proxy >/dev/null

for service in mcp-gateway mcp-jira mcp-confluence mcp-gitlab mcp-servicedesk mcp-opensearch; do
  log="$("${c[@]}" logs "$service" 2>&1)"
  grep -q 'event=devstack_profile' <<<"$log" || fail "$service did not log event=devstack_profile"
done
echo "apply-m4-switch: components run with profile devstack"
echo "apply-m4-switch: SIGNAL to ws-09: components of 4.0.0 run with the dev- keys; publish the bundle (.ci/switch-stack.sh --confirm)"
if [ "${M4_PUBLISHED:-0}" != 1 ]; then
  python3 "$root/scripts/m4.py" wait-bundle 600 || fail "the 4.0.0 bundle signed with the dev- key was not published within 10 minutes"
fi
"$root/scripts/proxy-test.sh" | tail -1
python3 "$root/scripts/m4.py" run
echo "apply-m4-switch: done; now: make m1, make m2, make m3 (m3 deletes the Authenticator of alice), mcp-e2e/run-m2.sh"
