#!/bin/sh
# Tomcat's remoteip valve takes a regex of proxy addresses. Compose networks have no fixed subnet here, so the
# address of the one proxy is resolved at start (a recreated proxy needs a restart of the BFF).
set -eu
if [ -n "${PORTAL_TRUSTED_PROXY_HOST:-}" ]; then
  address="$(getent hosts "$PORTAL_TRUSTED_PROXY_HOST" | awk 'NR == 1 { print $1 }')"
  [ -n "$address" ] || { echo "cannot resolve $PORTAL_TRUSTED_PROXY_HOST" >&2; exit 1; }
  PORTAL_TRUSTED_PROXIES="$(printf '%s' "$address" | sed 's/\./[.]/g')"
  export PORTAL_TRUSTED_PROXIES
fi
exec java -XX:MaxRAMPercentage=75 -XX:+ExitOnOutOfMemoryError -jar /app/app.jar
