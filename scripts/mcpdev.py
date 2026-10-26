#!/usr/bin/env python3
"""Dev helper for mcp-platform-dev (stdlib only).

  token USER [--client C] [--claims]         access token via authorization code + PKCE (headless login)
  exchange USER --audience A [--requester R] token exchange (RFC 8693) of USER's token to audience A
  put-secret USER SYSTEM [--secret S]        store a PAT for USER through the broker API (C4 PUT .../secret)

Configuration comes from .env and .secrets/dev.env; environment variables override:
  KEYCLOAK_URL   default http://localhost:$KEYCLOAK_PORT
  BROKER_URL     default https://localhost:$BROKER_MOCK_PORT (contract stub); real broker: https://localhost:$BROKER_PORT
  MCP_CERTS_DIR  default ./certs
"""

import argparse
import base64
import hashlib
import html.parser
import http.cookiejar
import json
import os
import secrets
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REALM = "corp"


def load_env_file(path):
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


CONFIG = {**load_env_file(ROOT / ".env"), **load_env_file(ROOT / ".secrets" / "dev.env"), **os.environ}


def cfg(key, default=None):
    value = CONFIG.get(key, default)
    if value is None:
        sys.exit(f"missing configuration {key} (run `make secrets`?)")
    return value


KEYCLOAK_URL = cfg("KEYCLOAK_URL", f"http://localhost:{cfg('KEYCLOAK_PORT', '18080')}")
OIDC = f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect"

# Redirect URIs registered in keycloak/realm-corp.json. They are never actually served: the login stops at
# the redirect and reads the code from the Location header, like a browser handing it to the client.
CLIENTS = {
    "claude-code": {
        "redirect_uri": f"http://localhost:{cfg('CLAUDE_CODE_CALLBACK_PORT', '33418')}/callback",
        "secret_key": None,
    },
    "mcp-portal-bff": {
        "redirect_uri": f"http://localhost:{cfg('PORTAL_PORT', '18090')}/login/oauth2/code/keycloak",
        "secret_key": "KC_CLIENT_MCP_PORTAL_BFF_SECRET",
    },
    "mcp-gateway": {"redirect_uri": None, "secret_key": "KC_CLIENT_MCP_GATEWAY_SECRET"},
}


class Fail(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class StopAtRedirectUri(urllib.request.HTTPRedirectHandler):
    """Keycloak answers an invalid authorization request (e.g. a scope the client may not ask for) by
    redirecting to the client with ?error=...; nothing listens there, so surface the error instead."""

    def __init__(self, redirect_uri):
        super().__init__()
        self.redirect_uri = redirect_uri

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl.startswith(self.redirect_uri):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(newurl).query)
            raise Fail(f"authorization request rejected: {params.get('error', ['?'])[0]}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class LoopbackCookiePolicy(http.cookiejar.DefaultCookiePolicy):
    # Keycloak marks its login cookies Secure; browsers send those to http://localhost, Python does not.
    def return_ok_secure(self, cookie, request):
        host = urllib.parse.urlparse(request.full_url).hostname
        return host in ("localhost", "127.0.0.1") or super().return_ok_secure(cookie, request)


class LoginFormParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == "kc-form-login":
            self.action = attrs.get("action")


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def decode_jwt(token):
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def client_auth(client_id):
    key = CLIENTS[client_id]["secret_key"]
    form = {"client_id": client_id}
    if key:
        form["client_secret"] = cfg(key)
    return form


def post_form(url, form):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise Fail(f"{url} -> HTTP {e.code}: {e.read().decode(errors='replace')}") from None


def user_password(user):
    return os.environ.get("MCP_DEV_PASSWORD") or cfg(f"KC_USER_{user.upper()}_PASSWORD")


def login(user, client_id="claude-code", scope="openid", password=None):
    """Authorization code + PKCE S256, filling the Keycloak login form without a browser."""
    client = CLIENTS[client_id]
    if not client["redirect_uri"]:
        raise Fail(f"client {client_id} has no browser flow")
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = b64url(secrets.token_bytes(16))
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": client["redirect_uri"],
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    jar = http.cookiejar.CookieJar(policy=LoopbackCookiePolicy())
    browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                          StopAtRedirectUri(client["redirect_uri"]))
    with browser.open(f"{OIDC}/auth?{query}") as resp:
        page = resp.read().decode()
    form = LoginFormParser()
    form.feed(page)
    if not form.action:
        raise Fail("no login form in Keycloak response (invalid client or redirect_uri?)")

    no_redirect = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
    body = urllib.parse.urlencode({"username": user, "password": password or user_password(user), "credentialId": ""}).encode()
    try:
        no_redirect.open(form.action, data=body)
        raise Fail(f"login of {user} failed (wrong password or required action)")
    except urllib.error.HTTPError as e:
        if e.code not in (302, 303):
            raise Fail(f"login of {user} failed: HTTP {e.code}") from None
        location = e.headers["Location"]
    params = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    if not location.startswith(client["redirect_uri"]) or params.get("state") != [state] or "code" not in params:
        raise Fail(f"unexpected redirect after login: {location.split('?')[0]}")

    return post_form(f"{OIDC}/token", {
        **client_auth(client_id),
        "grant_type": "authorization_code",
        "code": params["code"][0],
        "redirect_uri": client["redirect_uri"],
        "code_verifier": verifier,
    })["access_token"]


def exchange(subject_token, requester, audience):
    return post_form(f"{OIDC}/token", {
        **client_auth(requester),
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": subject_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": audience,
        # The audience mapper sits in an optional client scope, so a plain login token never carries it.
        "scope": f"{audience}-audience",
    })["access_token"]


def mtls_context(identity):
    certs = Path(cfg("MCP_CERTS_DIR", str(ROOT / "certs")))
    ctx = ssl.create_default_context(cafile=str(certs / "ca" / "ca.crt"))
    ctx.load_cert_chain(certs / identity / "tls.crt", certs / identity / "tls.key")
    return ctx


def put_secret(user, system, secret, expires_at=None, identity="mcp-portal-bff"):
    """What the portal BFF does for "Dodaj PAT": its session token exchanged for aud=mcp-credential-broker,
    sent over mTLS with the BFF certificate (C4). Returns (HTTP status, JSON body)."""
    bff_token = login(user, "mcp-portal-bff")
    broker_token = exchange(bff_token, "mcp-portal-bff", "mcp-credential-broker")
    broker_url = cfg("BROKER_URL", f"https://localhost:{cfg('BROKER_MOCK_PORT', '18443')}").rstrip("/")
    body = {"secret": secret}
    if expires_at:
        body["expiresAt"] = expires_at
    req = urllib.request.Request(
        f"{broker_url}/v1/connections/{urllib.parse.quote(system)}/secret",
        data=json.dumps(body).encode(), method="PUT")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {broker_token}")
    try:
        with urllib.request.urlopen(req, context=mtls_context(identity)) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:200]}
    except (urllib.error.URLError, ssl.SSLError, ConnectionError) as e:
        raise Fail(f"broker {broker_url} unreachable or rejected TLS: {getattr(e, 'reason', e)}") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("token")
    p.add_argument("user")
    p.add_argument("--client", default="claude-code", choices=["claude-code", "mcp-portal-bff"])
    p.add_argument("--claims", action="store_true", help="print decoded claims instead of the token")

    p = sub.add_parser("exchange")
    p.add_argument("user")
    p.add_argument("--audience", required=True)
    p.add_argument("--requester", default="mcp-portal-bff", choices=["mcp-portal-bff", "mcp-gateway"])
    p.add_argument("--claims", action="store_true")

    p = sub.add_parser("put-secret")
    p.add_argument("user")
    p.add_argument("system")
    p.add_argument("--secret", help="default: <user>-pat (the PAT the Jira mock knows)")
    p.add_argument("--expires-at")
    p.add_argument("--identity", default="mcp-portal-bff", help="client certificate under certs/")

    args = parser.parse_args()
    try:
        if args.cmd == "token":
            token = login(args.user, args.client)
            print(json.dumps(decode_jwt(token), indent=2) if args.claims else token)
        elif args.cmd == "exchange":
            # The gateway exchanges the AI client's token; the BFF exchanges its own session token.
            subject_client = "claude-code" if args.requester == "mcp-gateway" else "mcp-portal-bff"
            token = exchange(login(args.user, subject_client), args.requester, args.audience)
            print(json.dumps(decode_jwt(token), indent=2) if args.claims else token)
        elif args.cmd == "put-secret":
            status, body = put_secret(args.user, args.system, args.secret or f"{args.user}-pat",
                                      args.expires_at, args.identity)
            print(json.dumps({"httpStatus": status, **body}))
            return 0 if status == 200 else 1
    except Fail as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
