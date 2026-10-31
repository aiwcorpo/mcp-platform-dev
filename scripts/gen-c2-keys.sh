#!/usr/bin/env bash
# Private JWKS of the internal JWT (C2) shared by the gateway instances of M5: two ES256 keys (P-256), the first one signs, the
# second one is published for rotation. Without a shared file every instance generates its own keys and a receiver that
# verified a token against instance A fails on a token of instance B until it refetches the JWKS.
# Written to .secrets/c2-jwks.json (mode 600, git-ignored) or C2_JWKS_FILE; an existing file is kept. NOT FOR PRODUCTION.
# The kids do not start with test- or dev-, so the gateway accepts them without any relaxation. openssl runs in the pinned
# container of gen-certs.sh.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
out="${C2_JWKS_FILE:-$root/.secrets/c2-jwks.json}"
if [[ -s "$out" ]]; then
  echo "gen-c2-keys: $out exists, kept"
  exit 0
fi
image="alpine/openssl:3.5.9@sha256:1c361a204e9791bec6202bccbd1065eafaa65cb5a1a9a06ae000238569fba6a2"
umask 077
mkdir -p "$(dirname "$out")"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
for n in 1 2; do
  docker run --rm -i --user "$(id -u):$(id -g)" --entrypoint /bin/sh "$image" -s > "$tmp/key$n.txt" <<'SH'
set -e
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 2>/dev/null | openssl ec -noout -text 2>/dev/null
SH
done
python3 -I - "$tmp" "$out" <<'PY'
import base64, json, re, sys

def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

def hexblock(text, name):
    body = re.search(name + r":\s*\n((?:\s+[0-9a-f:]+\n)+)", text).group(1)
    return bytes.fromhex(re.sub(r"[^0-9a-f]", "", body))

keys = []
for n in (1, 2):
    text = open(f"{sys.argv[1]}/key{n}.txt").read()
    priv = hexblock(text, "priv").lstrip(b"\0").rjust(32, b"\0")
    pub = hexblock(text, "pub")
    assert len(priv) == 32 and len(pub) == 65 and pub[0] == 4, "unexpected openssl output"
    keys.append({"kty": "EC", "crv": "P-256", "alg": "ES256", "use": "sig", "kid": f"stack-c2-{n}",
                 "x": b64(pub[1:33]), "y": b64(pub[33:]), "d": b64(priv)})
with open(sys.argv[2], "w") as f:
    json.dump({"keys": keys}, f)
PY
chmod 600 "$out"
echo "gen-c2-keys: $out (active kid stack-c2-1)"
