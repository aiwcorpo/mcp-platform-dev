#!/usr/bin/env bash
# Tests of the load balancers (lb/gateway.conf, lb/broker.conf) against stand-in instances, in an isolated Docker network that
# does not touch the running stack: distribution over two instances, an unchanged request line and headers, no errors while
# one instance is killed, recovery when it returns, and the answer when both are gone.
# Host ports (loopback): 18758 gateway http, 18759 gateway tcp (8443), 18748 broker tcp (shared with scripts/proxy-test.sh,
# so the two do not run at the same time).
# Usage: scripts/lb-test.sh
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
net=mcp-dev-lbtest
gw_http=18758
gw_tcp=18759
br_tcp=18748
nginx_image="$(grep -o 'nginx:[^ ]*@sha256:[0-9a-f]*' "$root/docker-compose.yml" | head -1)"
python_image="$(grep -o 'python:[^ ]*@sha256:[0-9a-f]*' "$root/docker-compose.yml" | head -1)"
pass=0
fail=0
tmp="$(mktemp -d)"
names=("$net-gw-a" "$net-gw-b" "$net-br-a" "$net-br-b" "$net-gw-lb" "$net-br-lb")

remove_containers() {
  docker rm -f "${names[@]}" >/dev/null 2>&1
  [[ -f /.dockerenv ]] && docker network disconnect -f "$net" "$(hostname)" >/dev/null 2>&1
  docker network rm "$net" >/dev/null 2>&1
}
trap '[[ -n "${KEEP:-}" ]] || remove_containers; rm -rf "$tmp"' EXIT
remove_containers
docker network create "$net" >/dev/null || exit 1
# Inside a container (the CI runner) 127.0.0.1 is not the host: join the test network and reach the balancers by name.
if [[ -f /.dockerenv ]]; then
  docker network connect "$net" "$(hostname)" || exit 1
  curl() {
    command curl --connect-to "127.0.0.1:$gw_http:$net-gw-lb:8080" --connect-to "127.0.0.1:$gw_tcp:$net-gw-lb:8443" \
      --connect-to "127.0.0.1:$br_tcp:$net-br-lb:8443" "$@"
  }
fi

check() { # name expected actual
  if [[ "$2" == "$3" ]]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL $1: expected '$2', got '$3'"
  fi
}

# Stand-in instance: the echo server on 8080 (http) and 8443 (plain tcp, standing for the mTLS listener).
instance() { # container alias echo-name
  docker run -d --name "$1" --network "$net" --network-alias "$2" -e ECHO_NAME="$3" -v "$root/proxy/test/echo.py:/echo.py:ro" \
    "$python_image" sh -c 'ECHO_PORT=8443 python3 -I /echo.py & exec python3 -I /echo.py' >/dev/null || exit 1
}
lb() { # container conf publish...
  local name="$1" conf="$2"
  shift 2
  docker run -d --name "$name" --network "$net" --read-only --tmpfs /tmp --user 101:101 "$@" \
    -v "$root/lb/$conf:/etc/nginx/nginx.conf:ro" "$nginx_image" >/dev/null || exit 1
}
instance "$net-gw-a" gateway-backends.internal gw-a
instance "$net-gw-b" gateway-backends.internal gw-b
instance "$net-br-a" broker-backends.internal br-a
instance "$net-br-b" broker-backends.internal br-b
lb "$net-gw-lb" gateway.conf -p "127.0.0.1:$gw_http:8080" -p "127.0.0.1:$gw_tcp:8443"
lb "$net-br-lb" broker.conf -p "127.0.0.1:$br_tcp:8443"

gw="http://127.0.0.1:$gw_http"
up=0
for _ in $(seq 1 100); do
  if curl -sf "$gw/mcp" >/dev/null && curl -sf "http://127.0.0.1:$gw_tcp/" >/dev/null && curl -sf "http://127.0.0.1:$br_tcp/" >/dev/null; then up=1; break; fi
  sleep 0.3
done
if [[ "$up" != 1 ]]; then
  echo "load balancers did not start:" >&2
  docker logs "$net-gw-lb" 2>&1 | tail -5 >&2
  docker logs "$net-br-lb" 2>&1 | tail -5 >&2
  exit 1
fi

# served <url> <count> -> "name:count ..." sorted; failures counted as name "http-<status>".
served() {
  local url="$1" count="$2" out
  out="$(for _ in $(seq 1 "$count"); do
    body="$(curl -s -m 5 -w '\n%{http_code}' "$url")"
    code="${body##*$'\n'}"
    if [[ "$code" == 200 ]]; then jq -r .upstream <<<"${body%$'\n'*}"; else echo "http-$code"; fi
  done | sort | uniq -c | awk '{printf "%s:%s ", $2, $1}')"
  echo "${out% }"
}
count_of() { # name <served output>
  local n
  n="$(tr ' ' '\n' <<<"$2" | sed -n "s/^$1://p")"
  echo "${n:-0}"
}

# Distribution and pass-through, http.
result="$(served "$gw/mcp" 40)"
check "gateway http: both instances serve" 1 "$(( $(count_of gw-a "$result") >= 8 && $(count_of gw-b "$result") >= 8 ))"
check "gateway http: no other answers" 40 "$(( $(count_of gw-a "$result") + $(count_of gw-b "$result") ))"
curl -s -H 'Host: localhost:18745' -H 'X-Forwarded-Host: localhost:18745' -H 'X-Forwarded-Proto: https' -H 'X-Request-Id: 0198-lb' \
  -H 'Authorization: Bearer t' -H 'Mcp-Session-Id: s-1' "$gw/mcp?systems=gitlab,jira&access=read" > "$tmp/b"
check "gateway http: request line with the filter unchanged" "/mcp?systems=gitlab,jira&access=read" "$(jq -r .uri "$tmp/b")"
check "gateway http: Host unchanged" "localhost:18745" "$(jq -r .headers.host "$tmp/b")"
check "gateway http: forwarded headers unchanged" "localhost:18745 https" "$(jq -r '.headers["x-forwarded-host"] + " " + .headers["x-forwarded-proto"]' "$tmp/b")"
check "gateway http: Authorization, session and request id unchanged" "Bearer t s-1 0198-lb" \
  "$(jq -r '.headers.authorization + " " + .headers["mcp-session-id"] + " " + .headers["x-request-id"]' "$tmp/b")"
curl -s -D "$tmp/h" -o /dev/null "$gw/mcp?echo_status=401&echo_csp=1&echo_rid=gw-made"
check "gateway http: upstream status passed" 1 "$(grep -c '^HTTP/1.1 401' "$tmp/h")"
check "gateway http: upstream headers passed once" "1 1 1" \
  "$(tr -d '\r' < "$tmp/h" | tr 'A-Z' 'a-z' | awk -F: '/^content-security-policy:/{a++} /^x-content-type-options:/{b++} /^x-request-id:/{c++} END{print a, b, c}')"
curl -s -X POST -H 'Content-Type: application/json' --data-binary '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "$gw/mcp" > "$tmp/b"
check "gateway http: POST body reaches an instance" "POST" "$(jq -r .method "$tmp/b")"
curl -s -D "$tmp/h" -o "$tmp/b" -X POST --data-binary @<(head -c 5000000 /dev/zero | tr '\0' a) "$gw/mcp"
check "gateway http: 5 MB body refused with 413 and JSON" "413 invalid_request" "$(sed -n '1s/[^ ]* \([0-9]*\).*/\1/p' "$tmp/h") $(jq -r .error "$tmp/b")"
curl -s -D "$tmp/h" -o "$tmp/b" -H "X-Big: $(head -c 20000 /dev/zero | tr '\0' a)" "$gw/mcp"
check "gateway http: 20 KB header refused with 431 and JSON" "431 invalid_request" "$(sed -n '1s/[^ ]* \([0-9]*\).*/\1/p' "$tmp/h") $(jq -r .error "$tmp/b")"
curl -s -D "$tmp/h" -o "$tmp/b" -H "X-Big: $(head -c 9000 /dev/zero | tr '\0' a)" "$gw/mcp"
check "gateway http: a 9 KB header (cookie-sized) still passes" "200 gw" "$(sed -n '1s/[^ ]* \([0-9]*\).*/\1/p' "$tmp/h") $(jq -r '.upstream[0:2]' "$tmp/b")"

# TCP listeners.
result="$(served "http://127.0.0.1:$gw_tcp/" 40)"
check "gateway tcp: both instances serve" 1 "$(( $(count_of gw-a "$result") >= 8 && $(count_of gw-b "$result") >= 8 ))"
result="$(served "http://127.0.0.1:$br_tcp/" 40)"
check "broker tcp: both instances serve" 1 "$(( $(count_of br-a "$result") >= 8 && $(count_of br-b "$result") >= 8 ))"
check "health and status of the gateway balancer" "ok 200" "$(docker exec "$net-gw-lb" wget -q -O - http://127.0.0.1:8404/__lb/health | tr -d '\n') $(docker exec "$net-gw-lb" wget -q -S -O /dev/null http://127.0.0.1:8404/__lb/status 2>&1 | awk '/HTTP\//{print $2}')"

# One instance killed: no failed answers at all, everything served by the other one.
docker kill "$net-gw-a" "$net-br-a" >/dev/null
result="$(served "$gw/mcp" 60)"
check "gateway http: instance killed, no errors" "gw-b:60" "$result"
result="$(served "http://127.0.0.1:$gw_tcp/" 40)"
check "gateway tcp: instance killed, no errors" "gw-b:40" "$result"
result="$(served "http://127.0.0.1:$br_tcp/" 40)"
check "broker tcp: instance killed, no errors" "br-b:40" "$result"

# It returns (possibly with another address): traffic comes back within a few seconds, without restarting the balancer.
docker start "$net-gw-a" "$net-br-a" >/dev/null
back=0
for _ in $(seq 1 40); do
  result="$(served "$gw/mcp" 10)"
  result_br="$(served "http://127.0.0.1:$br_tcp/" 10)"
  if (( $(count_of gw-a "$result") > 0 && $(count_of br-a "$result_br") > 0 )); then back=1; break; fi
  sleep 0.5
done
check "instances back in rotation without restarting the balancers" 1 "$back"

# Both gone: the balancer answers itself.
docker kill "$net-gw-a" "$net-gw-b" "$net-br-a" "$net-br-b" >/dev/null
sleep 1.5
curl -s -m 5 -D "$tmp/h" -o "$tmp/b" "$gw/mcp"
check "gateway http: no instance -> 502 BalancerError (bad_gateway, fixed text, JSON utf-8, nosniff)" \
  "502 bad_gateway application/json; charset=utf-8 nosniff The request may or may not have been processed. Check the result before repeating a write." \
  "$(sed -n '1s/[^ ]* \([0-9]*\).*/\1/p' "$tmp/h") $(jq -r .error "$tmp/b") $(tr -d '\r' < "$tmp/h" | tr 'A-Z' 'a-z' | sed -n 's/^content-type: //p') $(tr -d '\r' < "$tmp/h" | tr 'A-Z' 'a-z' | sed -n 's/^x-content-type-options: //p') $(jq -r .message "$tmp/b")"
check "gateway balancer: 504 page is gateway_timeout, no non_idempotent retry, buffering off, read timeout >= 60 s" "1 0 1 1" \
  "$(grep -c 'gateway_timeout' "$root/lb/gateway.conf") $(grep -c 'non_idempotent' "$root/lb/gateway.conf") $(grep -c 'proxy_buffering off' "$root/lb/gateway.conf") $(grep -Ec 'proxy_read_timeout (6[0-9]|[7-9][0-9]|[0-9]{3,})s' "$root/lb/gateway.conf")"
curl -s -m 5 -o /dev/null "http://127.0.0.1:$gw_tcp/"; rc=$?
# 52 (empty reply) on the host, 56 (receive failure) through the in-network route of the runner: both mean the connection was closed.
check "gateway tcp: no instance -> connection closed" 1 "$(( rc == 52 || rc == 56 ))"
docker start "$net-gw-a" "$net-gw-b" >/dev/null
recovered=0
for _ in $(seq 1 40); do
  [[ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$gw/mcp")" == 200 ]] && { recovered=1; break; }
  sleep 0.5
done
check "gateway http: recovers after both instances restart" 1 "$recovered"

echo "lb tests: $pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
