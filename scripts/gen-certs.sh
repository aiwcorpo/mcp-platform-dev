#!/usr/bin/env bash
# Internal dev CA and mTLS certificates (C3, C4, C5). NOT FOR PRODUCTION.
# Runs openssl in a pinned container so the result does not depend on the host's openssl/LibreSSL.
# Existing files are kept; `make clean` removes them.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
certs="$root/certs"
secrets="$root/.secrets/dev.env"
image="alpine/openssl:3.5.9@sha256:1c361a204e9791bec6202bccbd1065eafaa65cb5a1a9a06ae000238569fba6a2"

[[ -f "$secrets" ]] || { echo "certs: run scripts/init-secrets.sh first" >&2; exit 1; }
store_password="$(grep '^TLS_STORE_PASSWORD=' "$secrets" | cut -d= -f2-)"
env_value() { grep "^$1=" "$root/.env" | cut -d= -f2-; }
broker_name="${MCP_BROKER_TLS_NAME:-$(env_value MCP_BROKER_TLS_NAME)}"
gateway_name="${MCP_GATEWAY_INTERNAL_TLS_NAME:-$(env_value MCP_GATEWAY_INTERNAL_TLS_NAME)}"

mkdir -p "$certs"

# <name>:<role>:<tls name>. CN=<name> is what the broker and gateway check on clients (C4: mcp-gateway,
# mcp-portal-bff, mcp-offboarding; C5: mcp-registry-ci-<serverId>, one certificate per MCP server, CC-161).
# <tls name> is the server name clients verify:
# <serverId>.internal for MCP servers (C5), configuration for the broker and gateway (CC-035).
# mcp-bulk is the stand-in of profile bulk (it answers to the name of mcp-confluence, whose identity it takes); mcp-e2e-stub, mcp-e2e-broker and e2e-prod-idp belong to profile e2e-prod and carry the
# names of the components they stand in for (mcp-jira.internal, the broker name), in a network of their own.
# Roles: server = serverAuth, client = clientAuth, both. Only server certificates get localhost in SAN,
# for components run on the host during development.
identities=(
  "mcp-gateway:both:$gateway_name"
  "mcp-credential-broker:server:$broker_name"
  "mcp-portal-bff:client:"
  "mcp-registry-ci-mcp-jira:client:"
  "mcp-registry-ci-mcp-confluence:client:"
  "mcp-registry-ci-mcp-gitlab:client:"
  "mcp-registry-ci-mcp-servicedesk:client:"
  "mcp-registry-ci-mcp-opensearch:client:"
  "mcp-offboarding:client:"
  "mcp-jira:server:mcp-jira.internal"
  "mcp-confluence:server:mcp-confluence.internal"
  "mcp-gitlab:server:mcp-gitlab.internal"
  "mcp-servicedesk:server:mcp-servicedesk.internal"
  "mcp-opensearch:server:mcp-opensearch.internal"
  "policies:server:policies.internal"
  "mcp-adapter-mcp-jira:client:"
  "mcp-bulk:server:mcp-confluence.internal"
  "mcp-e2e-stub:server:mcp-jira.internal"
  "mcp-e2e-broker:server:mcp-credential-broker.internal"
  "e2e-prod-idp:server:idp.e2e-prod.internal"
)

docker run --rm -i --user "$(id -u):$(id -g)" \
  -e STORE_PASSWORD="$store_password" -e GATEWAY_TLS_NAME="$gateway_name" \
  -v "$certs:/certs" -w /certs --entrypoint /bin/sh "$image" -s -- "${identities[@]}" <<'SH'
set -eu

new_ca() {
  dir="$1"; cn="$2"
  mkdir -p "$dir"
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$dir/ca.key" 2>/dev/null
  openssl req -x509 -new -key "$dir/ca.key" -sha256 -days 1825 \
    -subj "/O=mcp-platform-dev NOT FOR PRODUCTION/CN=$cn" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$dir/ca.crt"
}

issue() {
  ca="$1"; name="$2"; role="$3"; tls_name="$4"; dir="$5"
  mkdir -p "$dir"
  case "$role" in
    server) eku="serverAuth" ;;
    client) eku="clientAuth" ;;
    *)      eku="serverAuth,clientAuth" ;;
  esac
  san="DNS:$name"
  [ -n "$tls_name" ] && san="DNS:$tls_name,$san"
  [ "$role" != client ] && san="$san,DNS:localhost,IP:127.0.0.1"
  printf '%s\n' "basicConstraints=critical,CA:FALSE" "keyUsage=critical,digitalSignature" \
    "extendedKeyUsage=$eku" "subjectAltName=$san" > "$dir/ext.cnf"
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$dir/tls.key" 2>/dev/null
  openssl req -new -key "$dir/tls.key" -subj "/O=mcp-platform-dev NOT FOR PRODUCTION/CN=$name" -out "$dir/tls.csr"
  openssl x509 -req -in "$dir/tls.csr" -CA "$ca/ca.crt" -CAkey "$ca/ca.key" -CAcreateserial \
    -days 825 -sha256 -extfile "$dir/ext.cnf" -out "$dir/tls.crt" 2>/dev/null
  cat "$dir/tls.crt" "$ca/ca.crt" > "$dir/chain.crt"
  openssl pkcs12 -export -name "$name" -inkey "$dir/tls.key" -in "$dir/tls.crt" -certfile "$ca/ca.crt" \
    -passout env:STORE_PASSWORD -out "$dir/keystore.p12"
  rm -f "$dir/tls.csr" "$dir/ext.cnf"
}

# An existing CA is never regenerated (trust is already distributed); identities added to the list later are issued
# on the next run.
if [ ! -f ca/ca.crt ]; then
  new_ca ca "mcp-platform-dev internal CA"
  # -jdktrust marks the entry as trusted for Java; without it the JDK ignores certificates in PKCS12.
  openssl pkcs12 -export -nokeys -in ca/ca.crt -jdktrust anyExtendedKeyUsage -name mcp-dev-ca \
    -passout env:STORE_PASSWORD -out ca/truststore.p12
fi

for spec in "$@"; do
  name="${spec%%:*}"; rest="${spec#*:}"; role="${rest%%:*}"; tls_name="${rest#*:}"
  [ -f "$name/tls.crt" ] || issue ca "$name" "$role" "$tls_name" "$name"
done

# The shared CN of M3 (C5 before 4.0.0): the gateway answers 403 to it since CC-161, which the M4 checks verify. Never
# mounted into any container.
[ -f legacy/mcp-registry-ci/tls.crt ] || issue ca mcp-registry-ci client "" legacy/mcp-registry-ci

# Same CN and SAN as the gateway, signed by an unrelated CA: negative mTLS tests must see it rejected.
if [ ! -f untrusted-ca/ca.crt ]; then
  new_ca untrusted-ca "untrusted CA for negative tests"
  issue untrusted-ca mcp-gateway both "$GATEWAY_TLS_NAME" untrusted/mcp-gateway
fi

rm -f ca/ca.srl untrusted-ca/ca.srl
chmod 600 */*.key */*.p12 untrusted/*/*.key untrusted/*/*.p12 legacy/*/*.key legacy/*/*.p12
echo "Dev-only CA and keys generated by scripts/gen-certs.sh. Never use outside mcp-platform-dev." > NOT-FOR-PRODUCTION.txt
SH

echo "certs: wrote $certs"
