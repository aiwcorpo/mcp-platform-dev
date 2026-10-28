#!/usr/bin/env bash
# Tests of the reverse proxy rules (proxy/nginx.conf) against stand-in upstreams, in an isolated Docker network
# that does not touch the running stack. Every rule has a positive and a negative case.
# Usage: scripts/proxy-test.sh
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
net=mcp-dev-proxytest
port=18749
base="http://127.0.0.1:$port"
nginx_image="$(grep -o 'nginx:[^ ]*@sha256:[0-9a-f]*' "$root/docker-compose.yml" | head -1)"
python_image="$(grep -o 'python:[^ ]*@sha256:[0-9a-f]*' "$root/docker-compose.yml" | head -1)"
pass=0
fail=0
tmp="$(mktemp -d)"

remove_containers() {
  docker rm -f "$net-proxy" "$net-gateway" "$net-bff" "$net-web" >/dev/null 2>&1
  docker network rm "$net" >/dev/null 2>&1
}
trap '[[ -n "${KEEP:-}" ]] || remove_containers; rm -rf "$tmp"' EXIT
remove_containers
docker network create "$net" >/dev/null || exit 1

for pair in gateway:mcp-gateway bff:portal-bff web:portal-web; do
  name="${pair%%:*}"
  alias="${pair##*:}"
  docker run -d --name "$net-$name" --network "$net" --network-alias "$alias" -e ECHO_NAME="$name" \
    -v "$root/proxy/test/echo.py:/echo.py:ro" "$python_image" python3 -I /echo.py >/dev/null || exit 1
done
docker run -d --name "$net-proxy" --network "$net" --read-only --tmpfs /tmp --user 101:101 \
  -p "127.0.0.1:$port:8080" \
  -v "$root/proxy/nginx.conf:/etc/nginx/nginx.conf:ro" \
  -v "$root/proxy/proxy-common.conf:/etc/nginx/proxy-common.conf:ro" \
  -v "$root/proxy/to-bff.conf:/etc/nginx/to-bff.conf:ro" \
  -v "$root/proxy/to-gateway.conf:/etc/nginx/to-gateway.conf:ro" \
  -v "$root/proxy/to-web.conf:/etc/nginx/to-web.conf:ro" \
  -v "$root/proxy/gateway-headers.conf:/etc/nginx/gateway-headers.conf:ro" \
  -v "$root/proxy/errors:/etc/nginx/errors:ro" "$nginx_image" >/dev/null || exit 1

up=0
for _ in $(seq 1 100); do
  # the stand-ins need a moment longer than nginx
  if curl -sf "$base/__proxy/health" >/dev/null && curl -sf "$base/" >/dev/null && curl -sf "$base/api" >/dev/null \
     && curl -sf "$base/s/x" >/dev/null; then up=1; break; fi
  sleep 0.3
done
if [[ "$up" != 1 ]]; then
  echo "proxy did not start:" >&2
  docker logs "$net-proxy" 2>&1 | tail -5 >&2
  exit 1
fi

# req <curl args...>: response headers in $tmp/h (lowercased names), body in $tmp/b, status in $status.
req() {
  status="$(curl -s --path-as-is -D "$tmp/h.raw" -o "$tmp/b" -w '%{http_code}' "$@")"
  tr 'A-Z' 'a-z' < "$tmp/h.raw" | tr -d '\r' > "$tmp/h"
}
header() { grep -c "^$1:" "$tmp/h"; }
hvalue() { sed -n "s/^$1: //p" "$tmp/h" | head -1; }
field() { jq -r "$1" "$tmp/b" 2>/dev/null; }
# Name of the upstream that answered, "none" when the body is not an echo (an error page of the proxy).
upstream() { jq -r '.upstream // "none"' "$tmp/b" 2>/dev/null || echo none; }

check() { # name expected actual
  if [[ "$2" == "$3" ]]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL $1: expected '$2', got '$3'"
  fi
}

# Routing: each prefix reaches its upstream (positive), and does not reach the others (negative).
req "$base/";                         check "route / -> web" web "$(field .upstream)"
req "$base/catalog/abc";              check "route SPA path -> web" web "$(field .upstream)"
req "$base/api/v1/me";                check "route /api -> bff" bff "$(field .upstream)"
req "$base/api";                      check "route /api exact -> bff" bff "$(field .upstream)"
req "$base/apix";                     check "negative: /apix is not bff" web "$(field .upstream)"
req "$base/s/mcp-jira/mcp";           check "route /s/ -> gateway" gateway "$(field .upstream)"
req "$base/v/team-link/mcp";          check "route /v/ -> gateway" gateway "$(field .upstream)"
req "$base/s";                        check "negative: /s without slash is not gateway" web "$(field .upstream)"
req "$base/.well-known/oauth-protected-resource";
check "route well-known resource -> gateway" gateway "$(field .upstream)"
req "$base/.well-known/oauth-protected-resource/s/mcp-jira/mcp"
check "route well-known resource with path -> gateway" gateway "$(field .upstream)"
check "well-known path forwarded unchanged" "/.well-known/oauth-protected-resource/s/mcp-jira/mcp" "$(field .uri)"
req "$base/.well-known/jwks.json"
check "negative: other well-known is 404" 404 "$status"
check "negative: other well-known is not served by any upstream" none "$(upstream)"
req "$base/admin/v1/registry/servers"
check "negative: gateway admin path is 404" 404 "$status"
req -X POST "$base/api/v1/connections/gitlab/secret" -d '{}'
check "POST /api reaches bff" "POST bff" "$(field '.method') $(field .upstream)"

# Cookie: removed on gateway routes, kept for web and BFF.
for p in /s/mcp-jira/mcp /v/team-link/mcp /.well-known/oauth-protected-resource; do
  req -H 'Cookie: __Host-session=secret' "$base$p"
  check "Cookie removed on $p" null "$(field '.headers.cookie // "null"')"
done
req -H 'Cookie: __Host-session=secret' "$base/api/v1/me"
check "Cookie kept on /api" "__Host-session=secret" "$(field .headers.cookie)"
req -H 'Cookie: a=b' "$base/"
check "Cookie kept on /" "a=b" "$(field .headers.cookie)"
req -H 'Authorization: Bearer t' "$base/s/mcp-jira/mcp"
check "Authorization still reaches the gateway" "Bearer t" "$(field .headers.authorization)"

# Set-Cookie: dropped on gateway routes, kept elsewhere.
req "$base/s/mcp-jira/mcp?echo_cookie=1"
check "Set-Cookie dropped on /s/" 0 "$(header set-cookie)"
req "$base/.well-known/oauth-protected-resource?echo_cookie=1"
check "Set-Cookie dropped on well-known" 0 "$(header set-cookie)"
req "$base/api/v1/me?echo_cookie=1"
check "Set-Cookie kept on /api" 1 "$(header set-cookie)"
req "$base/?echo_cookie=1"
check "Set-Cookie kept on /" 1 "$(header set-cookie)"

# nosniff and CSP on every gateway response, errors included, exactly once; not added elsewhere.
csp="default-src 'none'; sandbox"
for p in /s/mcp-jira/mcp /v/team-link/mcp /.well-known/oauth-protected-resource; do
  for code in 200 401 404 500; do
    req "$base$p?echo_status=$code"
    check "nosniff on $p $code" "nosniff" "$(hvalue x-content-type-options)"
    check "CSP on $p $code" "$csp" "$(hvalue content-security-policy)"
  done
done
req "$base/s/mcp-jira/mcp?echo_csp=1"
check "upstream CSP replaced, not duplicated" 1 "$(header content-security-policy)"
check "upstream nosniff replaced, not duplicated" "nosniff" "$(hvalue x-content-type-options)"
req "$base/api/v1/me"
check "negative: no gateway CSP on /api" 0 "$(header content-security-policy)"
req "$base/"
check "negative: no gateway CSP on /" 0 "$(header content-security-policy)"

# Errors of the proxy itself on gateway routes are JSON, never HTML.
docker stop "$net-gateway" >/dev/null
req "$base/s/mcp-jira/mcp"
check "gateway down: status" 502 "$status"
check "gateway down: JSON content type" "application/json" "$(hvalue content-type)"
check "gateway down: JSON body" upstream_unavailable "$(field .error)"
check "gateway down: CSP" "$csp" "$(hvalue content-security-policy)"
docker start "$net-gateway" >/dev/null
for _ in $(seq 1 30); do
  req "$base/s/mcp-jira/mcp"
  [[ "$status" == 200 ]] && break
  sleep 0.5
done
check "gateway back after restart (no proxy restart)" 200 "$status"
req "$base/s/%zz"
check "bad escape on gateway route: JSON" "application/json" "$(hvalue content-type)"
req -X POST "$base/s/mcp-jira/mcp" -H 'Content-Type: application/json' --data-binary @<(head -c 5000000 /dev/zero | tr '\0' 'a')
check "oversized body on gateway route: 413" 413 "$status"
check "oversized body on gateway route: JSON" "application/json" "$(hvalue content-type)"
req "$base/missing-nothing/../../s/x"
check "negative: errors outside gateway routes are not JSON" "text/plain" "$(hvalue content-type | sed 's/;.*//')"
docker stop "$net-web" >/dev/null
req "$base/"
check "web down: 502" 502 "$status"
check "web down: not the gateway JSON" 0 "$(header content-security-policy)"
docker start "$net-web" >/dev/null

# Path normalization: only canonical paths are forwarded. Compare with a canonical request each time.
for p in "/s/../admin/v1/registry/servers" "/s/%2e%2e/admin/v1/registry/servers" "/s/%2E%2E/admin/v1/x" \
         "/s/mcp-jira/%2e%2e/%2e%2e/admin" "/s//mcp-jira/mcp" "/s/./mcp-jira/mcp" "/s/mcp-jira%2fmcp" \
         "/s/mcp-jira%5cmcp" "/v/x/..%2fadmin" "/api/../s/mcp-jira/mcp" "/api/%2e%2e/admin/v1/x" "/s/a%252e%252e/b" \
         "/s/mcp-jira/mcp%00"; do
  req "$base$p"
  check "non-canonical $p refused" 1 "$(( status == 400 || status == 404 ))"
  check "non-canonical $p never reached an upstream" none "$(upstream)"
done
req "$base/s/mcp-jira/mcp?next=http://a//b/../c"
check "positive: slashes and dots in the query are fine" 200 "$status"
check "positive: query forwarded unchanged" "/s/mcp-jira/mcp?next=http://a//b/../c" "$(field .uri)"
req "$base/s/a.b/c-d_e~f"
check "positive: dots inside a segment are fine" gateway "$(field .upstream)"

# X-Request-Id: passed as is in both directions, never generated or replaced by the proxy.
req -H 'X-Request-Id: 0198-client' "$base/api/v1/me"
check "request id passed to bff" "0198-client" "$(field '.headers["x-request-id"]')"
req "$base/api/v1/me"
check "no request id invented for bff" null "$(field '.headers["x-request-id"] // "null"')"
req "$base/api/v1/me?echo_rid=bff-generated"
check "bff response request id untouched" "bff-generated" "$(hvalue x-request-id)"
req "$base/s/mcp-jira/mcp?echo_rid=gw-generated"
check "gateway response request id untouched" "gw-generated" "$(hvalue x-request-id)"
req -H 'X-Request-Id: 0198-client' "$base/s/mcp-jira/mcp"
check "request id passed to gateway" "0198-client" "$(field '.headers["x-request-id"]')"

# Forwarded headers come from the proxy, not from the client.
req -H 'X-Forwarded-Host: evil.test' -H 'X-Forwarded-Proto: https' -H 'X-Forwarded-For: 6.6.6.6' -H 'Forwarded: host=evil.test' \
  -H 'X-Real-IP: 6.6.6.6' "$base/s/mcp-jira/mcp"
check "client X-Forwarded-Host replaced" "127.0.0.1:$port" "$(field '.headers["x-forwarded-host"]')"
check "client X-Forwarded-Proto replaced" http "$(field '.headers["x-forwarded-proto"]')"
check "client X-Forwarded-For replaced" 1 "$(field '.headers["x-forwarded-for"] | select(. != "6.6.6.6") | 1')"
check "client Forwarded dropped" null "$(field '.headers.forwarded // "null"')"
check "client X-Real-IP dropped" null "$(field '.headers["x-real-ip"] // "null"')"

# Methods and bodies pass through; the status of the upstream is not rewritten.
req -X DELETE "$base/s/mcp-jira/mcp?echo_status=204"
check "DELETE status passed" 204 "$status"
req "$base/s/mcp-jira/mcp?echo_status=401"
check "upstream 401 passed unchanged" 401 "$status"
check "upstream 401 body is the upstream's" gateway "$(field .upstream)"

echo "proxy tests: $pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
