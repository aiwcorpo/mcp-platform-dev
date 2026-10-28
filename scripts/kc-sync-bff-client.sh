#!/usr/bin/env bash
# Applies the redirect URIs of client mcp-portal-bff from .env to the RUNNING Keycloak (the realm file is only
# imported when the container is created, and recreating Keycloak would end every session). Idempotent.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
set -a
# shellcheck disable=SC1091
. "$root/.env"
. "$root/.secrets/dev.env"
set +a
kc="http://localhost:${KEYCLOAK_PORT}"

token="$(curl -sf "$kc/realms/master/protocol/openid-connect/token" -d grant_type=password -d client_id=admin-cli \
  --data-urlencode "username=$KC_BOOTSTRAP_ADMIN_USERNAME" --data-urlencode "password=$KC_BOOTSTRAP_ADMIN_PASSWORD" | jq -r .access_token)"
auth=(-H "Authorization: Bearer $token")
id="$(curl -sf "${auth[@]}" "$kc/admin/realms/corp/clients?clientId=mcp-portal-bff" | jq -r '.[0].id')"
client="$(curl -sf "${auth[@]}" "$kc/admin/realms/corp/clients/$id")"
updated="$(jq --arg portal "http://localhost:${PORTAL_PORT}" --arg proxy "http://localhost:${PROXY_PORT}" '
  .redirectUris = [$portal + "/login/oauth2/code/*", $portal + "/api/v1/oauth/callback", $proxy + "/api/v1/auth/callback"]
  | .attributes["post.logout.redirect.uris"] = ($portal + "/*##" + $proxy + "/*")' <<<"$client")"
curl -sf "${auth[@]}" -X PUT -H 'Content-Type: application/json' -d "$updated" "$kc/admin/realms/corp/clients/$id"
echo "mcp-portal-bff redirect URIs: $(jq -c .redirectUris <<<"$updated")"
