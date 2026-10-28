#!/usr/bin/env bash
# Certificates of the public HTTPS origins (proxy: https://localhost:PROXY_TLS_PORT, Keycloak:
# https://localhost:KEYCLOAK_TLS_PORT) from a separate "dev public" CA.
# The internal CA (scripts/gen-certs.sh) signs mTLS identities and has no name restrictions; trusting it in the
# system keychain would make it valid for any host. This CA is limited by name constraints to localhost and
# 127.0.0.1, so it is the one a person adds to the macOS keychain (make trust-ca). NOT FOR PRODUCTION.
# Existing files are kept (an existing CA is never regenerated, the person has already trusted it); the CA key
# stays in certs/public-ca/ (git-ignored) and is never mounted anywhere. certs/public-ca/truststore.p12 holds
# only this CA, for Java clients of the public Keycloak address.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
certs="$root/certs"
image="alpine/openssl:3.5.9@sha256:1c361a204e9791bec6202bccbd1065eafaa65cb5a1a9a06ae000238569fba6a2"

secrets="$root/.secrets/dev.env"
[[ -f "$secrets" ]] || { echo "certs: run scripts/init-secrets.sh first" >&2; exit 1; }
STORE_PASSWORD="$(grep '^TLS_STORE_PASSWORD=' "$secrets" | cut -d= -f2-)"
export STORE_PASSWORD
mkdir -p "$certs"

docker run --rm -i --user "$(id -u):$(id -g)" -e STORE_PASSWORD -v "$certs:/certs" -w /certs --entrypoint /bin/sh "$image" -s <<'SH'
set -eu
subject="/O=mcp-platform-dev NOT FOR PRODUCTION"
mkdir -p public-ca proxy keycloak

if [ ! -f public-ca/ca.crt ]; then
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out public-ca/ca.key 2>/dev/null
  openssl req -x509 -new -key public-ca/ca.key -sha256 -days 1825 -subj "$subject/CN=mcp-platform-dev public CA (localhost only)" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "nameConstraints=critical,permitted;DNS:localhost,permitted;IP:127.0.0.1/255.255.255.255,permitted;IP:::1/ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff" \
    -out public-ca/ca.crt
  chmod 600 public-ca/ca.key
  echo "certs: wrote public-ca"
fi

# 397 days: the longest lifetime Safari accepts for a server certificate.
leaf() {
  dir="$1"
  [ -f "$dir/tls.crt" ] && return 0
  printf '%s\n' "basicConstraints=critical,CA:FALSE" "keyUsage=critical,digitalSignature" "extendedKeyUsage=serverAuth" \
    "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1" > "$dir/ext.cnf"
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$dir/tls.key" 2>/dev/null
  openssl req -new -key "$dir/tls.key" -subj "$subject/CN=localhost" -out "$dir/tls.csr"
  openssl x509 -req -in "$dir/tls.csr" -CA public-ca/ca.crt -CAkey public-ca/ca.key -CAcreateserial \
    -days 397 -sha256 -extfile "$dir/ext.cnf" -out "$dir/tls.crt" 2>/dev/null
  cat "$dir/tls.crt" public-ca/ca.crt > "$dir/chain.crt"
  rm -f "$dir/tls.csr" "$dir/ext.cnf" public-ca/ca.srl
  chmod 644 "$dir/tls.key"
  echo "certs: wrote $dir"
}
leaf proxy
leaf keycloak

if [ ! -f public-ca/truststore.p12 ]; then
  openssl pkcs12 -export -nokeys -in public-ca/ca.crt -jdktrust anyExtendedKeyUsage -name mcp-dev-public-ca \
    -passout env:STORE_PASSWORD -out public-ca/truststore.p12
  chmod 644 public-ca/truststore.p12
  echo "certs: wrote public-ca/truststore.p12"
fi
SH
