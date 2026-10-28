#!/usr/bin/env bash
# Certificate of the public HTTPS origin (https://localhost:PROXY_TLS_PORT) from a separate "dev public" CA.
# The internal CA (scripts/gen-certs.sh) signs mTLS identities and has no name restrictions; trusting it in the
# system keychain would make it valid for any host. This CA is limited by name constraints to localhost and
# 127.0.0.1, so it is the one a person adds to the macOS keychain (make trust-ca). NOT FOR PRODUCTION.
# Existing files are kept; the CA key stays in certs/public-ca/ (git-ignored) and is never mounted anywhere.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
certs="$root/certs"
image="alpine/openssl:3.5.9@sha256:1c361a204e9791bec6202bccbd1065eafaa65cb5a1a9a06ae000238569fba6a2"

if [[ -f "$certs/proxy/tls.crt" && -f "$certs/public-ca/ca.crt" ]]; then
  echo "certs: $certs/proxy/tls.crt exists, keeping it"
  exit 0
fi
mkdir -p "$certs"

docker run --rm -i --user "$(id -u):$(id -g)" -v "$certs:/certs" -w /certs --entrypoint /bin/sh "$image" -s <<'SH'
set -eu
subject="/O=mcp-platform-dev NOT FOR PRODUCTION"
mkdir -p public-ca proxy

openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out public-ca/ca.key 2>/dev/null
openssl req -x509 -new -key public-ca/ca.key -sha256 -days 1825 -subj "$subject/CN=mcp-platform-dev public CA (localhost only)" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "nameConstraints=critical,permitted;DNS:localhost,permitted;IP:127.0.0.1/255.255.255.255,permitted;IP:::1/ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff" \
  -out public-ca/ca.crt

# 397 days: the longest lifetime Safari accepts for a server certificate.
printf '%s\n' "basicConstraints=critical,CA:FALSE" "keyUsage=critical,digitalSignature" "extendedKeyUsage=serverAuth" \
  "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1" > proxy/ext.cnf
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out proxy/tls.key 2>/dev/null
openssl req -new -key proxy/tls.key -subj "$subject/CN=localhost" -out proxy/tls.csr
openssl x509 -req -in proxy/tls.csr -CA public-ca/ca.crt -CAkey public-ca/ca.key -CAcreateserial \
  -days 397 -sha256 -extfile proxy/ext.cnf -out proxy/tls.crt 2>/dev/null
cat proxy/tls.crt public-ca/ca.crt > proxy/chain.crt
rm -f proxy/tls.csr proxy/ext.cnf public-ca/ca.srl
chmod 600 public-ca/ca.key
chmod 644 proxy/tls.key
SH
echo "certs: wrote $certs/public-ca and $certs/proxy"
