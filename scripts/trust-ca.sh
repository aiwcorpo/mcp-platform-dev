#!/usr/bin/env bash
# Prints what a person runs to trust the "dev public" CA (certs/public-ca/ca.crt). Changes nothing itself: adding a
# trust anchor to the keychain is the user's decision. The CA is limited by name constraints to localhost.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
ca="$root/certs/public-ca/ca.crt"
[[ -f "$ca" ]] || { echo "trust-ca: $ca is missing, run make certs" >&2; exit 1; }
port="$(grep '^PROXY_TLS_PORT=' "$root/.env" | cut -d= -f2-)"
cat <<TXT
CA: $ca
SHA-256: $(openssl x509 -in "$ca" -noout -fingerprint -sha256 | cut -d= -f2)

1. Safari and Chrome on macOS (login keychain; asks for the login password or Touch ID):

   security add-trusted-cert -r trustRoot -p ssl -k "\$HOME/Library/Keychains/login.keychain-db" "$ca"

   Remove later with:

   security remove-trusted-cert "$ca"

2. Claude Code (Node does not read the macOS keychain; set it for the shell that starts claude):

   export NODE_EXTRA_CA_CERTS="$ca"

3. Check: curl --cacert "$ca" -sI https://localhost:$port/__proxy/health
TXT
