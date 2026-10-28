#!/usr/bin/env python3
"""Dev helper for mcp-platform-dev (stdlib only).

  token USER [--client C] [--claims]         access token via authorization code + PKCE (headless login)
  exchange USER --audience A [--requester R] token exchange (RFC 8693) of USER's token to audience A
  put-secret USER SYSTEM                     store a PAT for USER through the broker API (C4 PUT .../secret);
                                             the PAT comes from $MCP_PAT, default <user>-pat
  audit REQUEST_ID [--logs FILE]... [--compose]
                                             C8 audit events of one request from gateway, broker, mcp-jira logs
  jira-auth                                  credentials that reached the Jira mock (from its request journal)

Configuration comes from .env and .secrets/dev.env; environment variables override:
  KEYCLOAK_URL         default https://localhost:$KEYCLOAK_TLS_PORT (certificate of certs/public-ca)
  BROKER_URL           default https://localhost:$BROKER_MOCK_PORT (contract stub); real broker: https://localhost:$BROKER_PORT
  MCP_BROKER_TLS_NAME  name verified in the broker certificate, whatever host BROKER_URL uses (CC-035)
  MCP_CERTS_DIR        default ./certs
"""

import argparse
import base64
import hashlib
import html.parser
import http.client
import http.cookiejar
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
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


KEYCLOAK_URL = cfg("KEYCLOAK_URL", f"https://localhost:{cfg('KEYCLOAK_TLS_PORT', '18746')}")
OIDC = f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect"

# The public address of Keycloak has a certificate of the "dev public" CA; urlopen and the opener below trust it.
_public_ca = ROOT / "certs" / "public-ca" / "ca.crt"
HTTPS = urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(_public_ca)) if _public_ca.exists()
                                    else ssl.create_default_context())
urllib.request.install_opener(urllib.request.build_opener(HTTPS))

# Redirect URIs registered in keycloak/realm-corp.json. They are never actually served: the login stops at
# the redirect and reads the code from the Location header, like a browser handing it to the client.
CLIENTS = {
    "claude-code": {
        "redirect_uri": f"http://localhost:{cfg('CLAUDE_CODE_CALLBACK_PORT', '33418')}/callback",
        "secret_key": None,
    },
    "mcp-portal-bff": {
        "redirect_uri": f"http://localhost:{cfg('PORTAL_PORT', '18090')}/api/v1/auth/callback",
        "secret_key": "KC_CLIENT_MCP_PORTAL_BFF_SECRET",
    },
    "mcp-gateway": {"redirect_uri": None, "secret_key": "KC_CLIENT_MCP_GATEWAY_SECRET"},
    "mcp-credential-broker": {"redirect_uri": None, "secret_key": "KC_CLIENT_MCP_CREDENTIAL_BROKER_SECRET"},
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
    browser = urllib.request.build_opener(HTTPS, urllib.request.HTTPCookieProcessor(jar),
                                          StopAtRedirectUri(client["redirect_uri"]))
    with browser.open(f"{OIDC}/auth?{query}") as resp:
        page = resp.read().decode()
    form = LoginFormParser()
    form.feed(page)
    if not form.action:
        raise Fail("no login form in Keycloak response (invalid client or redirect_uri?)")

    no_redirect = urllib.request.build_opener(HTTPS, urllib.request.HTTPCookieProcessor(jar), NoRedirect)
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


def exchange(subject_token, requester, audience=None):
    form = {
        **client_auth(requester),
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": subject_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
    }
    if audience:
        form["audience"] = audience
        # The audience mapper sits in an optional client scope, so a plain login token never carries it.
        form["scope"] = f"{audience}-audience"
    return post_form(f"{OIDC}/token", form)["access_token"]


def mtls_context(identity):
    certs = Path(cfg("MCP_CERTS_DIR", str(ROOT / "certs")))
    ctx = ssl.create_default_context(cafile=str(certs / "ca" / "ca.crt"))
    ctx.load_cert_chain(certs / identity / "tls.crt", certs / identity / "tls.key")
    return ctx


class PinnedNameHTTPSConnection(http.client.HTTPSConnection):
    """Connects to host:port from the URL but verifies the certificate against tls_name, so a broker
    reached as localhost from the host is still checked for its configured name (CC-035)."""

    def __init__(self, host, port, tls_name, context):
        super().__init__(host, port, context=context, timeout=30)
        self.tls_name = tls_name

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.tls_name)


def put_secret(user, system, secret, expires_at=None, identity="mcp-portal-bff", tls_name=None):
    """What the portal BFF does for "Dodaj PAT": its session token exchanged for aud=mcp-credential-broker,
    sent over mTLS with the BFF certificate (C4). Returns (HTTP status, JSON body)."""
    bff_token = login(user, "mcp-portal-bff")
    broker_token = exchange(bff_token, "mcp-portal-bff", "mcp-credential-broker")
    broker_url = cfg("BROKER_URL", f"https://localhost:{cfg('BROKER_MOCK_PORT', '18443')}").rstrip("/")
    tls_name = tls_name or cfg("MCP_BROKER_TLS_NAME", "mcp-credential-broker.internal")
    url = urllib.parse.urlparse(broker_url)
    if url.scheme != "https":
        raise Fail("BROKER_URL must be https (C4 requires mTLS)")
    body = {"secret": secret}
    if expires_at:
        body["expiresAt"] = expires_at
    conn = PinnedNameHTTPSConnection(url.hostname, url.port or 443, tls_name, mtls_context(identity))
    try:
        conn.request("PUT", f"{url.path}/v1/connections/{urllib.parse.quote(system)}/secret",
                     body=json.dumps(body).encode(),
                     headers={"Content-Type": "application/json; charset=utf-8",
                              "Authorization": f"Bearer {broker_token}"})
        resp = conn.getresponse()
        raw = resp.read().decode(errors="replace")
    except (OSError, ssl.SSLError, http.client.HTTPException) as e:
        raise Fail(f"broker {broker_url} (TLS name {tls_name}) unreachable or rejected TLS: "
                   f"{getattr(e, 'reason', None) or e}") from None
    finally:
        conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, {"raw": raw[:200]}


def broker_request(user, method, path, body=None, broker_url=None, tls_name=None):
    """A C4 call as the portal BFF makes it for `user` (session token exchanged for aud=mcp-credential-broker, mTLS
    with the BFF certificate). Returns (HTTP status, JSON body or {})."""
    broker_token = exchange(login(user, "mcp-portal-bff"), "mcp-portal-bff", "mcp-credential-broker")
    url = urllib.parse.urlparse((broker_url or cfg("BROKER_URL", f"https://localhost:{cfg('BROKER_PORT', '18100')}")).rstrip("/"))
    tls_name = tls_name or cfg("MCP_BROKER_TLS_NAME", "mcp-credential-broker.internal")
    conn = PinnedNameHTTPSConnection(url.hostname, url.port or 443, tls_name, mtls_context("mcp-portal-bff"))
    try:
        conn.request(method, f"{url.path}{path}", body=json.dumps(body).encode() if body is not None else None,
                     headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {broker_token}"})
        resp = conn.getresponse()
        raw = resp.read().decode(errors="replace")
    except (OSError, ssl.SSLError, http.client.HTTPException) as e:
        raise Fail(f"broker unreachable or rejected TLS: {getattr(e, 'reason', None) or e}") from None
    finally:
        conn.close()
    try:
        return resp.status, json.loads(raw) if raw else {}
    except ValueError:
        return resp.status, {"raw": raw[:200]}


AUDIT_COMPONENTS = {"gateway": "mcp-gateway", "broker": "mcp-credential-broker", "server": "mcp-jira"}
AUDIT_REQUIRED = ("ts", "requestId", "component", "componentId", "event")


def audit_events(lines, request_id):
    """C8 events (one JSON object per line, possibly after a log prefix) carrying request_id."""
    events = []
    for line in lines:
        if request_id not in line or "{" not in line:
            continue
        try:
            event = json.loads(line[line.index("{"):])
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("requestId") == request_id:
            events.append(event)
    return events


def compose_log_lines():
    out = subprocess.run([str(ROOT / "scripts" / "compose.sh"), "--profile", "full", "logs", "--no-color",
                          "--no-log-prefix", *AUDIT_COMPONENTS.values()], capture_output=True, text=True)
    if out.returncode != 0:
        raise Fail(f"compose logs failed: {out.stderr.strip()[-300:]}")
    return out.stdout.splitlines()


def audit(request_id, logs_files=None, with_compose=None):
    """Events from all given log files merged; container logs are read when no file is given or when
    with_compose is set (components on the host write to files, the rest run in compose)."""
    if isinstance(logs_files, (str, Path)):
        logs_files = [logs_files]
    logs_files = list(logs_files or [])
    if with_compose is None:
        with_compose = not logs_files
    lines = []
    for path in logs_files:
        try:
            lines += Path(path).read_text(errors="replace").splitlines()
        except OSError as e:
            raise Fail(f"cannot read log file {path}: {e.strerror}") from None
    if with_compose:
        lines += compose_log_lines()
    events = audit_events(lines, request_id)
    for e in sorted(events, key=lambda e: e.get("ts", "")):
        missing = [k for k in AUDIT_REQUIRED if k not in e]
        print(f"{e.get('ts', '?'):26} {e.get('component', '?'):8} {e.get('componentId', '?'):22} "
              f"{e.get('event', '?'):20} {e.get('decision') or e.get('outcome') or ''}"
              f"{'  MISSING ' + ','.join(missing) if missing else ''}")
    seen = {e.get("component") for e in events}
    absent = [c for c in AUDIT_COMPONENTS if c not in seen]
    incomplete = [e for e in events if any(k not in e for k in AUDIT_REQUIRED)]
    print(f"components with events: {sorted(seen) or 'none'}; missing: {absent or 'none'}")
    return 0 if not absent and not incomplete else 1


def jira_auth():
    """Summary of the Authorization headers the Jira mock received, newest first. Known test PATs are
    named, an IdP JWT is flagged (token passthrough, invariant 2), anything else is shown by length only."""
    known = {"alice-pat", "bob-pat", "carol-pat", "mallory-pat", "test-only-jira-pat-0000000000000000"}
    url = f"http://localhost:{cfg('JIRA_MOCK_PORT', '18081')}/__admin/requests?limit=50"
    basic = base64.b64encode(f"admin:{cfg('JIRA_MOCK_ADMIN_PASSWORD')}".encode()).decode()
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Basic {basic}"})) as resp:
        journal = json.load(resp)
    rows = []
    for entry in journal.get("requests", []):
        req = entry["request"]
        auth = next((v for k, v in req.get("headers", {}).items() if k.lower() == "authorization"), None)
        if auth is None:
            kind = "none"
        elif auth.startswith("Bearer ") and auth[7:] in known:
            kind = f"PAT {auth[7:]}"
        elif re.fullmatch(r"Bearer [\w-]+\.[\w-]+\.[\w-]*", auth):
            claims = decode_jwt(auth[7:])
            kind = f"JWT iss={claims.get('iss')} aud={claims.get('aud')} (passthrough!)"
        else:
            kind = f"{auth.split(' ')[0]} <{len(auth)} chars>"
        rows.append({"method": req["method"], "url": req["url"], "status": entry["responseDefinition"]["status"],
                     "credential": kind})
    return rows


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
    p.add_argument("--expires-at")
    p.add_argument("--identity", default="mcp-portal-bff", help="client certificate under certs/")

    p = sub.add_parser("audit")
    p.add_argument("request_id")
    p.add_argument("--logs", action="append", default=[], metavar="FILE",
                   help="log file to search (repeatable); without --compose only these files are read")
    p.add_argument("--compose", action="store_true",
                   help="also read container logs of gateway, broker and mcp-jira (default when no --logs)")

    sub.add_parser("jira-auth")

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
            secret = os.environ.get("MCP_PAT") or f"{args.user}-pat"
            status, body = put_secret(args.user, args.system, secret, args.expires_at, args.identity)
            print(json.dumps({"httpStatus": status, **body}))
            return 0 if status == 200 else 1
        elif args.cmd == "audit":
            return audit(args.request_id, args.logs, True if args.compose else None)
        elif args.cmd == "jira-auth":
            for row in jira_auth():
                print(f"{row['status']:4} {row['method']:6} {row['url'][:60]:60} {row['credential']}")
    except Fail as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
