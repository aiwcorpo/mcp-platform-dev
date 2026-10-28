#!/usr/bin/env bash
# Applies redirect URIs and post-logout URIs of client mcp-portal-bff from keycloak/realm-corp.json (the single
# source; ${PORTAL_PORT} and ${PROXY_PORT} come from .env) to the RUNNING Keycloak: the realm file is only
# imported when the container is created, and recreating Keycloak would end every session. Idempotent.
# Secrets reach curl through /dev/fd files, never through argv.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
set -a
# shellcheck disable=SC1091
. "$root/.env"
. "$root/.secrets/dev.env"
set +a
kc="http://localhost:${KEYCLOAK_PORT}"

wanted="$(jq -c '.clients[] | select(.clientId == "mcp-portal-bff") | {redirectUris, post: .attributes["post.logout.redirect.uris"]}' \
  "$root/keycloak/realm-corp.json" | sed -e "s/\${PORTAL_PORT}/${PORTAL_PORT}/g" -e "s/\${PROXY_PORT}/${PROXY_PORT}/g")"

token="$(curl -sf "$kc/realms/master/protocol/openid-connect/token" \
  -d @<(jq -rn --arg u "$KC_BOOTSTRAP_ADMIN_USERNAME" --arg p "$KC_BOOTSTRAP_ADMIN_PASSWORD" \
    '"grant_type=password&client_id=admin-cli&username=\($u|@uri)&password=\($p|@uri)"') | jq -r .access_token)"
auth() { printf 'header = "Authorization: Bearer %s"\n' "$token"; }
id="$(curl -sf -K <(auth) "$kc/admin/realms/corp/clients?clientId=mcp-portal-bff" | jq -r '.[0].id')"
client="$(curl -sf -K <(auth) "$kc/admin/realms/corp/clients/$id")"
updated="$(jq --argjson w "$wanted" '.redirectUris = $w.redirectUris | .attributes["post.logout.redirect.uris"] = $w.post' <<<"$client")"
curl -sf -K <(auth) -X PUT -H 'Content-Type: application/json' -d @<(printf '%s' "$updated") "$kc/admin/realms/corp/clients/$id"
echo "mcp-portal-bff redirect URIs: $(jq -c .redirectUris <<<"$updated"); post-logout: $(jq -r '.attributes["post.logout.redirect.uris"]' <<<"$updated")"
