#!/usr/bin/env python3
"""Dev helper for mcp-platform-dev (stdlib only).

  token USER [--client C] [--claims] [--acr mfa]
                                             access token via authorization code + PKCE (headless login)
  otp USER [--uri]                           current TOTP code of a test user (dev seed), or the otpauth URI
  exchange USER --audience A [--requester R] token exchange (RFC 8693) of USER's token to audience A
  put-secret USER SYSTEM                     store a PAT for USER through the broker API (C4 PUT .../secret);
                                             the PAT comes from $MCP_PAT, default <user>-pat
  reset-authenticator USER                   remove the Authenticator registration of USER at the broker (mTLS CN=mcp-offboarding)
  audit REQUEST_ID [--logs FILE]... [--compose]
                                             C8 audit events of one request from gateway, broker, mcp-jira logs
  jira-auth                                  credentials that reached the Jira mock (from its request journal)
  offboard LOGIN [--dry-run]                 IdP (groups, logout), broker purge, gateway purge (CN=mcp-offboarding), purges repeated
  sync-groups                                add the groups and memberships of keycloak/realm-corp.json to the running realm (additive)
  registry-put SERVER_ID MANIFEST            C5 PUT of a manifest with the certificate of that server (CN=mcp-registry-ci-<serverId>)

Configuration comes from .env and .secrets/dev.env; environment variables override:
  KEYCLOAK_URL         default https://localhost:$KEYCLOAK_TLS_PORT (certificate of certs/public-ca)
  BROKER_URL           default https://localhost:$BROKER_MOCK_PORT (contract stub); real broker: https://localhost:$BROKER_PORT
  MCP_BROKER_TLS_NAME  name verified in the broker certificate, whatever host BROKER_URL uses (CC-035)
  GATEWAY_ADMIN_URL    default https://localhost:$GATEWAY_INTERNAL_PORT (C5 listener of the gateway, mTLS)
  MCP_GATEWAY_INTERNAL_TLS_NAME  name verified in the gateway certificate
  KEYCLOAK_ADMIN_URL   default http://localhost:$KEYCLOAK_PORT (admin API; admin login from .secrets/dev.env)
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import totp  # noqa: E402
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
    """Action of the Keycloak form with the given id: kc-form-login (password) or kc-otp-login-form (OTP)."""

    def __init__(self, form_id="kc-form-login"):
        super().__init__()
        self.form_id = form_id
        self.action = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == self.form_id:
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


# Throw-away users (TempUser) are not in .secrets/dev.env: their password and Base32 OTP seed live here for the run.
EXTRA_USERS = {}


def user_password(user):
    if user in EXTRA_USERS:
        return EXTRA_USERS[user]["password"]
    return os.environ.get("MCP_DEV_PASSWORD") or cfg(f"KC_USER_{user.upper()}_PASSWORD")


def otp_seed(user):
    if user in EXTRA_USERS:
        return EXTRA_USERS[user]["otp_seed"]
    return cfg(f"KC_USER_{user.upper()}_OTP_SEED")


def login(user, client_id="claude-code", scope="openid", password=None, acr_values=None, otp=False, max_age=None,
          otp_code=None):
    """Authorization code + PKCE S256, filling the Keycloak login form without a browser.

    acr_values="mfa" asks for the step-up level of the realm (password + OTP); the OTP is generated from the seed of
    the user in .secrets/dev.env (otp=True does the same when the realm asks for it without acr_values)."""
    client = CLIENTS[client_id]
    if not client["redirect_uri"]:
        raise Fail(f"client {client_id} has no browser flow")
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = b64url(secrets.token_bytes(16))
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": client["redirect_uri"],
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if acr_values:
        params["acr_values"] = acr_values
    if max_age is not None:
        params["max_age"] = str(max_age)
    query = urllib.parse.urlencode(params)
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

    def submit(action, fields):
        """(Location of the redirect, None) or (None, HTML of the next form)."""
        try:
            with no_redirect.open(action, data=urllib.parse.urlencode(fields).encode()) as resp:
                return None, resp.read().decode()
        except urllib.error.HTTPError as e:
            if e.code not in (302, 303):
                raise Fail(f"login of {user} failed: HTTP {e.code}") from None
            return e.headers["Location"], None

    location, page = submit(form.action, {"username": user, "password": password or user_password(user), "credentialId": ""})
    if location is None:
        otp_form = LoginFormParser("kc-otp-login-form")
        otp_form.feed(page)
        if not otp_form.action:
            raise Fail(f"login of {user} failed (wrong password or required action)")
        if not (acr_values or otp):
            raise Fail(f"Keycloak asked {user} for an OTP; pass acr_values/otp")
        location, page = submit(otp_form.action, {"otp": otp_code or totp.fresh_code(otp_seed(user)), "login": "Sign In"})
        if location is None:
            raise Fail(f"OTP of {user} rejected")
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


def mtls_request(identity, method, base_url, path, tls_name, body=None):
    """A call over mTLS with the certificate certs/<identity> and no bearer token (C4 and C5 admin operations of CI and
    offboarding). The name verified in the server certificate is tls_name, whatever host base_url uses (CC-035).
    Returns (HTTP status, JSON body or {})."""
    url = urllib.parse.urlparse(base_url.rstrip("/"))
    conn = PinnedNameHTTPSConnection(url.hostname, url.port or 443, tls_name, mtls_context(identity))
    headers = {"X-Request-Id": str(uuid7())}
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    try:
        conn.request(method, f"{url.path}{path}", body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode(errors="replace")
    except (OSError, ssl.SSLError, http.client.HTTPException) as e:
        raise Fail(f"{base_url} (TLS name {tls_name}) unreachable or rejected TLS: {getattr(e, 'reason', None) or e}") from None
    finally:
        conn.close()
    try:
        return resp.status, json.loads(raw) if raw else {}
    except ValueError:
        return resp.status, {"raw": raw[:200]}


def broker_admin_url():
    return cfg("BROKER_URL", f"https://localhost:{cfg('BROKER_PORT', '18100')}")


def gateway_admin_url():
    return cfg("GATEWAY_ADMIN_URL", f"https://localhost:{cfg('GATEWAY_INTERNAL_PORT', '18445')}")


def offboarding_request(method, path, broker_url=None, tls_name=None):
    """A C4 admin call as the offboarding / helpdesk client makes it: mTLS with CN=mcp-offboarding and no bearer token."""
    return mtls_request("mcp-offboarding", method, broker_url or broker_admin_url(), path,
                        tls_name or cfg("MCP_BROKER_TLS_NAME", "mcp-credential-broker.internal"))


def gateway_request(identity, method, path, body=None):
    """A C5 call straight to the gateway listener (mTLS only); identity is the directory of the client certificate."""
    return mtls_request(identity, method, gateway_admin_url(), path,
                        cfg("MCP_GATEWAY_INTERNAL_TLS_NAME", "mcp-gateway.internal"), body)


def registry_put(server_id, manifest_path, identity=None):
    """PUT /admin/v1/registry/servers/{serverId} with the manifest built from the reviewed commit (mcp-app/target/mcp-manifest.json).
    The certificate is the one of that server: CN=mcp-registry-ci-<serverId> (CC-161); `identity` overrides it for tests
    that present another certificate on purpose."""
    return gateway_request(identity or f"mcp-registry-ci-{server_id}", "PUT",
                           f"/admin/v1/registry/servers/{urllib.parse.quote(server_id, safe='')}",
                           Path(manifest_path).read_bytes())


KC_ADMIN_URL = cfg("KEYCLOAK_ADMIN_URL", f"http://localhost:{cfg('KEYCLOAK_PORT', '18080')}")


def kc_admin(method, path, body=None):
    """Admin REST API of Keycloak (realm corp) as the bootstrap admin; the credentials never appear in argv or output.
    Returns (HTTP status, JSON body or {})."""
    token = post_form(f"{KC_ADMIN_URL}/realms/master/protocol/openid-connect/token",
                      {"grant_type": "password", "client_id": "admin-cli", "username": cfg("KC_BOOTSTRAP_ADMIN_USERNAME"),
                       "password": cfg("KC_BOOTSTRAP_ADMIN_PASSWORD")})["access_token"]
    req = urllib.request.Request(f"{KC_ADMIN_URL}/admin/realms/{REALM}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode(errors="replace")[:200]}


SHARED_TEST_USERS = ("alice", "bob", "carol", "mallory")


class TempUser:
    """A throw-away realm user for scenarios that register an Authenticator or burn confirmation codes: random name, password and an
    OTP credential Keycloak accepts for the step-up. Deleted, and purged at broker and gateway, on exit. Never one of SHARED_TEST_USERS."""

    def __init__(self, groups, otp=True):
        self.name = "m4-" + secrets.token_hex(4)
        self.password = secrets.token_urlsafe(18)
        self.raw_otp = secrets.token_hex(10)
        self.groups = groups
        self.otp = otp
        self.sub = None

    def __enter__(self):
        credentials = [{"type": "password", "value": self.password, "temporary": False}]
        if self.otp:
            credentials.append({"type": "otp", "secretData": json.dumps({"value": self.raw_otp}),
                                "credentialData": json.dumps({"subType": "totp", "digits": 6, "period": 30, "algorithm": "HmacSHA1", "counter": 0})})
        status, body = kc_admin("POST", "/users", {"username": self.name, "enabled": True, "emailVerified": True, "email": f"{self.name}@corp.test",
                                                    "firstName": "M4", "lastName": "Check", "credentials": credentials})
        if status != 201:
            raise Fail(f"cannot create the temporary user: HTTP {status} {body}")
        self.sub = kc_admin("GET", f"/users?username={self.name}&exact=true")[1][0]["id"]
        for group in self.groups:
            found = kc_admin("GET", f"/groups?search={group}&exact=true")[1]
            kc_admin("PUT", f"/users/{self.sub}/groups/{next(g['id'] for g in found if g['name'] == group)}")
        EXTRA_USERS[self.name] = {"password": self.password, "otp_seed": base64.b32encode(self.raw_otp.encode()).decode()}
        return self

    def __exit__(self, *exc):
        EXTRA_USERS.pop(self.name, None)
        quoted = urllib.parse.quote(self.sub, safe="")
        for call in (lambda: offboarding_request("POST", f"/v1/admin/users/{quoted}/purge"),
                     lambda: gateway_request("mcp-offboarding", "POST", f"/admin/v1/users/{quoted}/purge")):
            try:
                call()
            except Fail:
                pass
        kc_admin("DELETE", f"/users/{self.sub}")

    def token(self, client="claude-code"):
        return login(self.name, client, password=self.password)


def sync_groups():
    """Brings the RUNNING realm to the groups and memberships of keycloak/realm-corp.json (the file is imported only when the
    container is created, and recreating Keycloak ends every session). Additive: groups and memberships are added, never removed.
    Returns the list of changes made."""
    realm = json.loads((ROOT / "keycloak" / "realm-corp.json").read_text())
    changes = []
    existing = {g["name"]: g["id"] for g in kc_admin("GET", "/groups?max=200")[1]}
    for group in realm["groups"]:
        if group["name"] not in existing:
            if kc_admin("POST", "/groups", {"name": group["name"]})[0] != 201:
                raise Fail(f"cannot create group {group['name']}")
            existing[group["name"]] = next(g["id"] for g in kc_admin("GET", f"/groups?search={group['name']}&exact=true")[1] if g["name"] == group["name"])
            changes.append(f"group {group['name']} created")
    for user in realm["users"]:
        status, found = kc_admin("GET", f"/users?username={user['username']}&exact=true")
        if status != 200 or not found:
            raise Fail(f"user {user['username']} not in the realm")
        sub = found[0]["id"]
        have = {g["name"] for g in kc_admin("GET", f"/users/{sub}/groups")[1]}
        for path in user.get("groups", []):
            name = path.lstrip("/")
            if name not in have:
                if kc_admin("PUT", f"/users/{sub}/groups/{existing[name]}")[0] != 204:
                    raise Fail(f"cannot add {user['username']} to {name}")
                changes.append(f"{user['username']} added to {name}")
    return changes




def offboard(login_name, dry_run=False, repeat_delay=5.0, allow_shared=False):
    """Offboarding of one user (README of the contracts, "Retencja i offboarding"): three independent, idempotent calls plus the
    repeat the contract recommends after the logout. Order: IdP (groups removed so tokens stop being renewed, sessions ended
    so the back-channel logout reaches the portal), broker purge, gateway purge, then both purges again. A failing step does
    not stop the others. Returns a list of (step, ok, detail); the caller turns it into an exit code."""
    import time
    if login_name in SHARED_TEST_USERS and not allow_shared and not dry_run:
        raise Fail(f"{login_name} is a shared test user (alice, bob, carol, mallory): offboarding removes its groups; "
                   "use another login or --allow-shared")
    steps = []

    def step(name, ok, detail=""):
        steps.append((name, ok, detail))

    status, users = kc_admin("GET", f"/users?username={urllib.parse.quote(login_name)}&exact=true")
    if status != 200 or not isinstance(users, list) or not users:
        raise Fail(f"user {login_name} not found in the realm (HTTP {status})")
    sub = users[0]["id"]
    status, groups = kc_admin("GET", f"/users/{sub}/groups")
    groups = groups if isinstance(groups, list) else []
    if dry_run:
        step("idp: groups that would be removed", True, ", ".join(g["name"] for g in groups) or "none")
        step("idp: sessions that would be ended", True, "")
        step("broker: POST /v1/admin/users/{sub}/purge", True, f"sub {sub}, not sent")
        step("gateway: POST /admin/v1/users/{sub}/purge", True, f"sub {sub}, not sent")
        return steps
    removed = [g["name"] for g in groups if kc_admin("DELETE", f"/users/{sub}/groups/{g['id']}")[0] == 204]
    step("idp: groups removed", len(removed) == len(groups), f"{len(removed)} of {len(groups)}: {', '.join(removed) or 'none'}")
    status, _ = kc_admin("POST", f"/users/{sub}/logout")
    step("idp: sessions ended (back-channel logout of the portal)", status in (200, 204), f"HTTP {status}")

    quoted = urllib.parse.quote(sub, safe="")

    def purges(label):
        for name, call in (("broker", lambda: offboarding_request("POST", f"/v1/admin/users/{quoted}/purge")),
                           ("gateway", lambda: gateway_request("mcp-offboarding", "POST", f"/admin/v1/users/{quoted}/purge"))):
            try:
                status, body = call()
                step(f"{name}: purge{label}", status == 204, f"HTTP {status}" + ("" if status == 204 else f" {body}"))
            except Fail as e:
                step(f"{name}: purge{label}", False, str(e))

    purges("")
    time.sleep(repeat_delay)
    purges(" (repeated after the logout)")
    return steps


def uuid7():
    """UUIDv7 (RFC 9562) for X-Request-Id."""
    import time
    import uuid
    ms = int(time.time() * 1000)
    value = (ms << 80) | (0x7 << 76) | (secrets.randbits(12) << 64) | (0b10 << 62) | secrets.randbits(62)
    return uuid.UUID(int=value)


def reset_authenticator(user, force=False):
    """POST /v1/admin/users/{sub}/authenticator/reset for a test user (lost phone and lost IdP factor): removes the
    registration and a pending enrollment. A verification lock stays (it lifts only with time). `sub` comes from a
    real login, so it is whatever the IdP issues, not a value copied from the realm file.
    alice, bob, carol and mallory hold the Authenticators of people (a test run once removed one): refused without force."""
    if user in SHARED_TEST_USERS and not force:
        raise Fail(f"{user} is a shared test user whose Authenticator belongs to a person; refusing to reset it "
                   "(make reset-authenticator USER=... FORCE=1 is the human's tool)")
    sub = decode_jwt(login(user))["sub"]
    return offboarding_request("POST", f"/v1/admin/users/{urllib.parse.quote(sub, safe='')}/authenticator/reset")


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


def jira_auth(limit=50):
    """Summary of the Authorization headers the Jira mock received, newest first. Known test PATs are
    named, an IdP JWT is flagged (token passthrough, invariant 2), anything else is shown by length only."""
    known = {"alice-pat", "bob-pat", "carol-pat", "mallory-pat", "test-only-jira-pat-0000000000000000"}
    url = f"http://localhost:{cfg('JIRA_MOCK_PORT', '18081')}/__admin/requests?limit={limit}"
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
    p.add_argument("--acr", help="acr_values to request, e.g. mfa (password + OTP of the dev seed)")

    p = sub.add_parser("otp")
    p.add_argument("user")
    p.add_argument("--uri", action="store_true", help="print the otpauth URI of the seed (to add it to a real app) instead of the code")

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

    p = sub.add_parser("reset-authenticator")
    p.add_argument("user")
    p.add_argument("--force", action="store_true", help="permit alice, bob, carol, mallory (the Authenticator of a person is removed)")

    p = sub.add_parser("offboard")
    p.add_argument("login")
    p.add_argument("--dry-run", action="store_true", help="resolve the user and print the plan, change nothing")
    p.add_argument("--allow-shared", action="store_true", help="permit alice, bob, carol, mallory (they lose their groups)")
    p.add_argument("--repeat-delay", type=float, default=5.0, help="seconds between the purges and their repetition")

    p = sub.add_parser("registry-put")
    p.add_argument("server_id")
    p.add_argument("manifest")
    p.add_argument("--identity", help="client certificate directory under certs/ (default mcp-registry-ci-<serverId>)")

    sub.add_parser("sync-groups")
    sub.add_parser("jira-auth")

    args = parser.parse_args()
    try:
        if args.cmd == "token":
            token = login(args.user, args.client, acr_values=args.acr)
            print(json.dumps(decode_jwt(token), indent=2) if args.claims else token)
        elif args.cmd == "otp":
            if args.uri:
                print(f"otpauth://totp/MCP%20Platform%20dev:{args.user}?secret={otp_seed(args.user)}&issuer=MCP%20Platform%20dev"
                      "&algorithm=SHA1&digits=6&period=30")
            else:
                print(f"{totp.fresh_code(otp_seed(args.user))}  (valid for {int(totp.seconds_left())} s)")
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
        elif args.cmd == "reset-authenticator":
            status, body = reset_authenticator(args.user, args.force)
            print(json.dumps({"httpStatus": status, **body}))
            if status == 204:
                return 0
            if status in (404, 405):
                print("this broker build has no Authenticator reset (M3, contracts 3.0.1)", file=sys.stderr)
            return 1
        elif args.cmd == "sync-groups":
            for change in sync_groups() or ["realm already matches keycloak/realm-corp.json"]:
                print(change)
        elif args.cmd == "offboard":
            steps = offboard(args.login, args.dry_run, args.repeat_delay, args.allow_shared)
            for name, ok, detail in steps:
                print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
            return 0 if all(ok for _, ok, _ in steps) else 1
        elif args.cmd == "registry-put":
            status, body = registry_put(args.server_id, args.manifest, args.identity)
            print(json.dumps({"httpStatus": status, **body}))
            return 0 if status in (200, 201) else 1
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
