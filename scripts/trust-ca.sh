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

After the CA was regenerated (scripts/gen-public-cert.sh) the old one must go and the new one must be added; both have the same name, so
   delete by SHA-1. List what the keychain holds and find the SHA-1 of the old entry:

   security find-certificate -a -c "mcp-platform-dev public CA" -Z "\$HOME/Library/Keychains/login.keychain-db" | grep -E 'SHA-1|alis'

   Then, for every SHA-1 that is not the one of the current CA ($(openssl x509 -in "$ca" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d :)):

   security delete-certificate -Z <SHA-1 of the old CA> "\$HOME/Library/Keychains/login.keychain-db"

   and run command 1 above for the new CA.

   Claude Code reads NODE_EXTRA_CA_CERTS from the env block of ~/.claude/settings.json before the variable of the shell. If that block sets
   it (to an old file, say), the export in command 2 has no effect; the macOS keychain (command 1) is the more reliable way, or fix the value there.

3. Check: curl --cacert "$ca" -sI https://localhost:$port/__proxy/health
TXT
