#!/usr/bin/env python3
"""M2 scenario skeleton ("Polacz konto i portal") and helpers for the system mocks.

  m2.py run              checks that can be made on the mocks and Keycloak today; the steps that need the real
                         BFF, web, broker OAuth and gateway M2 are listed as SKIP with the missing piece
  m2.py mock-auth NAME   which credentials reached a mock (gitlab, confluence, servicedesk, opensearch)

The GitLab OAuth checks play the broker's part (authorization code + PKCE S256 + state against the mock), so a
broker that fails the same flow can be compared against them. Exit code 0 when nothing FAILed.
"""
import base64
import hashlib
import json
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

import mcpdev
from mcpdev import cfg

PORTS = {name: int(cfg(f"{name.upper()}_MOCK_PORT")) for name in ("gitlab", "confluence", "servicedesk", "opensearch")}
PROXY = f"http://localhost:{cfg('PROXY_PORT')}"
GITLAB = f"http://localhost:{PORTS['gitlab']}"
CALLBACK = f"{PROXY}/api/v1/oauth/callback"
CLIENT = {"client_id": "mcp-broker", "client_secret": None}
results = []


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def http(method, url, headers=None, data=None):
    """(status, headers, body bytes); redirects are returned, not followed."""
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    if isinstance(data, dict):
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with OPENER.open(req, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except urllib.error.URLError:
        return 0, {}, b""


def jbody(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def record(name, ok, detail=""):
    results.append(("PASS" if ok else "FAIL", name, detail))


def skip(name, reason):
    results.append(("SKIP", name, reason))


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def pkce():
    verifier = mcpdev.b64url(secrets.token_bytes(32))
    return verifier, mcpdev.b64url(hashlib.sha256(verifier.encode()).digest())


def authorize(challenge, state, **extra):
    query = {"client_id": CLIENT["client_id"], "response_type": "code", "redirect_uri": CALLBACK, "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256", "scope": "api", **extra}
    status, headers, _ = http("GET", f"{GITLAB}/oauth/authorize?{urllib.parse.urlencode(query)}")
    location = headers.get("Location")
    params = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(location).query).items()} if location else {}
    return status, location, params


def token(form):
    status, _, raw = http("POST", f"{GITLAB}/oauth/token", data={**CLIENT, **form})
    return status, jbody(raw)


def whoami_gitlab(access):
    status, _, raw = http("GET", f"{GITLAB}/api/v4/user", bearer(access))
    return status, jbody(raw).get("username")


def check_mocks():
    for name, port in PORTS.items():
        status, _, _ = http("GET", f"http://localhost:{port}/__mock/health")
        record(f"mock {name} healthy on :{port}", status == 200)

    for pat, expected in (("alice-pat", "alice"), ("bob-pat", "bob")):
        record(f"gitlab whoami {pat} -> {expected}", whoami_gitlab(pat) == (200, expected))
        status, _, raw = http("GET", f"http://localhost:{PORTS['confluence']}/rest/api/user/current", bearer(pat))
        record(f"confluence whoami {pat} -> {expected}", status == 200 and jbody(raw).get("username") == expected)
    record("gitlab whoami of an unknown token -> 401", whoami_gitlab("nobody-pat")[0] == 401)
    status, _, raw = http("GET", f"http://localhost:{PORTS['confluence']}/rest/api/user/current", bearer("nobody-pat"))
    record("confluence whoami of an unknown token -> someone-else", status == 200 and jbody(raw).get("username") == "someone-else")

    idp = mcpdev.login("alice")
    record("gitlab refuses an IdP token (no passthrough)", whoami_gitlab(idp)[0] == 401)
    status, _, _ = http("GET", f"http://localhost:{PORTS['confluence']}/rest/api/content/10001", bearer(idp))
    record("confluence refuses an IdP token on data", status == 401)

    status, _, raw = http("GET", f"{GITLAB}/api/v4/projects/team-a%2Fapp", bearer("alice-pat"))
    record("gitlab project team-a/app readable with alice-pat", status == 200 and jbody(raw).get("id") == 42)
    status, _, _ = http("GET", f"{GITLAB}/api/v4/projects/team-b%2Fsecret", bearer("alice-pat"))
    record("gitlab project team-b/secret -> 404 for alice (same as missing)", status == 404)
    status, _, raw = http("GET", f"http://localhost:{PORTS['confluence']}/rest/api/content/10003?expand=body.storage", bearer("alice-pat"))
    record("confluence page 10003 readable", status == 200 and "body" in jbody(raw))

    sd = f"http://localhost:{PORTS['servicedesk']}/api/v1/tickets/IT-1"
    status, _, _ = http("GET", sd)
    record("servicedesk without the service key -> 401", status == 401)
    status, _, raw = http("GET", sd, bearer(cfg("SERVICEDESK_API_KEY")))
    record("servicedesk ticket IT-1 with the service key", status == 200 and jbody(raw).get("queue") == "IT")

    gateway_token = mcpdev.login("alice", "claude-code")
    broker_token = mcpdev.exchange(gateway_token, "mcp-gateway", "mcp-credential-broker")
    os_token = mcpdev.exchange(broker_token, "mcp-credential-broker", "opensearch")
    base = f"http://localhost:{PORTS['opensearch']}"
    query = json.dumps({"query": {"match_all": {}}, "size": 1}).encode()
    status, _, _ = http("POST", f"{base}/logs-team-a-*/_search", {**bearer(os_token), "Content-Type": "application/json"}, query)
    record("opensearch logs-team-a-* with an exchanged token", status == 200)
    status, _, _ = http("POST", f"{base}/hr-payroll/_search", {**bearer(os_token), "Content-Type": "application/json"}, query)
    record("opensearch hr-payroll -> 403 for alice", status == 403)
    status, _, _ = http("POST", f"{base}/logs-team-a-*/_search", {**bearer(gateway_token), "Content-Type": "application/json"}, query)
    record("opensearch refuses the gateway-audience token", status == 401)


def check_gitlab_oauth():
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(12)
    status, _, params = authorize(challenge, state, mock_user="alice")
    record("oauth: authorize approves and echoes state", status == 302 and params.get("state") == state and "code" in params)
    grant = {"grant_type": "authorization_code", "code": params.get("code", ""), "redirect_uri": CALLBACK, "code_verifier": verifier}
    status, tokens = token(grant)
    record("oauth: code + PKCE verifier -> tokens", status == 200 and tokens.get("refresh_token", "").startswith("glr-"))
    record("oauth: whoami of the OAuth token -> alice", whoami_gitlab(tokens.get("access_token", "")) == (200, "alice"))

    status, rotated = token({"grant_type": "refresh_token", "refresh_token": tokens.get("refresh_token", "")})
    record("oauth: refresh gives new tokens", status == 200 and rotated.get("access_token") != tokens.get("access_token"))
    record("oauth: the replaced access token stops working", whoami_gitlab(tokens.get("access_token", ""))[0] == 401)
    record("oauth: the replaced refresh token is invalid_grant", token({"grant_type": "refresh_token", "refresh_token": tokens.get("refresh_token", "")})[1].get("error") == "invalid_grant")

    _, _, params = authorize(challenge, "s0", mock_user="alice")
    again = {**grant, "code": params["code"], "code_verifier": verifier}
    first = token(again)[1]
    record("oauth: authorization code is single use", token(again)[0] == 400)
    record("oauth: reusing a code voids the tokens it produced", whoami_gitlab(first.get("access_token", ""))[0] == 401)

    verifier, challenge = pkce()
    _, _, params = authorize(challenge, "s1", mock_user="alice")
    status, body = token({**grant, "code": params["code"], "code_verifier": mcpdev.b64url(secrets.token_bytes(32))})
    record("oauth: wrong PKCE verifier -> invalid_grant", status == 400 and body.get("error") == "invalid_grant")

    _, _, params = authorize(challenge, "s2", mock_user="bob")
    status, body = token({**grant, "code": params["code"], "code_verifier": verifier})
    record("oauth: authorizing as bob gives a token whose whoami is bob",
           status == 200 and whoami_gitlab(body.get("access_token", "")) == (200, "bob"))

    status, location, params = authorize(challenge, "s3", mock_action="deny")
    record("oauth: user denies -> error=access_denied (portal: cancelled)", status == 302 and params.get("error") == "access_denied" and params.get("state") == "s3")
    status, _, params = authorize(challenge, "s4", mock_user="alice", mock_scenario="wrong_state")
    record("oauth: wrong_state scenario returns a different state", status == 302 and params.get("state") not in (None, "s4") and "code" in params)
    status, _, params = authorize(challenge, "s5", mock_user="alice", mock_scenario="provider_error")
    record("oauth: provider_error scenario -> error=server_error", params.get("error") == "server_error" and "code" not in params)
    _, _, params = authorize(challenge, "s6", mock_user="alice", mock_scenario="token_error")
    record("oauth: token_error scenario -> 500 at the token endpoint", token({**grant, "code": params["code"], "code_verifier": verifier})[0] == 500)
    _, _, params = authorize(challenge, "s7", mock_user="alice", mock_scenario="refresh_revoked")
    _, body = token({**grant, "code": params["code"], "code_verifier": verifier})
    record("oauth: refresh_revoked scenario -> invalid_grant on refresh", token({"grant_type": "refresh_token", "refresh_token": body["refresh_token"]})[1].get("error") == "invalid_grant")

    query = urllib.parse.urlencode({"client_id": "mcp-broker", "response_type": "code", "redirect_uri": "http://localhost:9/cb",
                                    "state": "x", "code_challenge": challenge, "code_challenge_method": "S256"})
    status, headers, _ = http("GET", f"{GITLAB}/oauth/authorize?{query}")
    record("oauth: unregistered redirect_uri -> 400 without redirect", status == 400 and "Location" not in headers)
    status, _, _ = http("POST", f"{GITLAB}/oauth/token", data={**grant, "client_id": "mcp-broker", "client_secret": "wrong"})
    record("oauth: wrong client secret -> 401", status == 401)


def check_broker_oauth():
    """"Polacz przez OAuth" as the BFF drives it, against the real broker (needs make up-full, broker M2 image)."""
    status, body = mcpdev.broker_request("alice", "POST", "/v1/connections/gitlab/oauth/start", {"returnPath": "/catalog"})
    if status == 404 or (status == 400 and body.get("error") != "invalid_request"):
        skip("broker OAuth against the GitLab mock", f"broker without oauth/start for gitlab (HTTP {status})")
        return
    record("broker: oauth/start returns authorizeUrl and flowId", status == 200 and "flowId" in body, f"HTTP {status}")
    if status != 200:
        return
    url = urllib.parse.urlparse(body["authorizeUrl"])
    query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
    record("broker: authorizeUrl points at the mock with PKCE S256, state and the portal callback",
           url.netloc == f"localhost:{PORTS['gitlab']}" and url.path == "/oauth/authorize"
           and query.get("code_challenge_method") == "S256" and query.get("redirect_uri") == CALLBACK
           and query.get("client_id") == "mcp-broker" and query.get("state") and query.get("code_challenge"))

    def approve(authorize_url, **extra):
        status, headers, _ = http("GET", authorize_url + "&" + urllib.parse.urlencode(extra))
        loc = headers.get("Location", "")
        return status, {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).items()}

    _, cb = approve(body["authorizeUrl"], mock_user="alice")
    status, done = mcpdev.broker_request("alice", "POST", "/v1/connections/gitlab/oauth/complete",
                                         {"flowId": body["flowId"], "code": cb.get("code", ""), "state": cb.get("state", "")})
    record("broker: complete -> connected as alice", status == 200 and done.get("backendUser") == "alice", f"HTTP {status} {done}")
    status, listing = mcpdev.broker_request("alice", "GET", "/v1/connections")
    record("broker: connection listed as connected", status == 200 and any(
        c.get("system") == "gitlab" and c.get("status") == "connected" for c in listing))

    _, start = mcpdev.broker_request("alice", "POST", "/v1/connections/gitlab/oauth/start", {"returnPath": "/catalog"})
    _, cb = approve(start["authorizeUrl"], mock_user="bob")
    status, done = mcpdev.broker_request("alice", "POST", "/v1/connections/gitlab/oauth/complete",
                                         {"flowId": start["flowId"], "code": cb.get("code", ""), "state": cb.get("state", "")})
    record("broker: another GitLab account is accepted and reported as backendUser (no identity comparison, contracts 2.2.3)",
           status == 200 and done.get("backendUser") == "bob", f"HTTP {status} {done}")

    _, start = mcpdev.broker_request("alice", "POST", "/v1/connections/gitlab/oauth/start", {"returnPath": "/catalog"})
    _, cb = approve(start["authorizeUrl"], mock_user="alice", mock_scenario="wrong_state")
    status, done = mcpdev.broker_request("alice", "POST", "/v1/connections/gitlab/oauth/complete",
                                         {"flowId": start["flowId"], "code": cb.get("code", ""), "state": cb.get("state", "")})
    record("broker: tampered state -> 422 flow_invalid", status == 422 and done.get("error") == "flow_invalid", f"HTTP {status} {done}")

    _, start = mcpdev.broker_request("alice", "POST", "/v1/connections/gitlab/oauth/start", {"returnPath": "/catalog"})
    _, cb = approve(start["authorizeUrl"], mock_action="deny")
    record("broker flow: user denies -> the callback carries error=access_denied (BFF: cancelled, broker not called)",
           cb.get("error") == "access_denied" and cb.get("state") == dict(urllib.parse.parse_qsl(urllib.parse.urlparse(start["authorizeUrl"]).query))["state"])

    status, _ = mcpdev.broker_request("alice", "DELETE", "/v1/connections/gitlab")
    record("broker: disconnect -> 204 (token revoked at the provider)", status == 204, f"HTTP {status}")


def redis_as(user, commands):
    """Replies of redis-cli run as ACL user; password and commands travel through stdin only."""
    password = cfg(f"REDIS_{user.upper().replace('-', '_')}_PASSWORD")
    container = subprocess.run([str(mcpdev.ROOT / "scripts" / "compose.sh"), "ps", "-q", "redis"], capture_output=True,
                               text=True).stdout.strip()
    script = 'read -r REDISCLI_AUTH; export REDISCLI_AUTH; redis-cli --no-auth-warning --user ' + user
    out = subprocess.run(["docker", "exec", "-i", container, "sh", "-c", script], capture_output=True, text=True,
                         input=password + "\n" + "\n".join(commands) + "\n")
    return [line for line in out.stdout.splitlines() if line.strip()]


def check_redis_acl():
    forbidden = ["flushall", "flushdb", "swapdb 0 1", "keys '*'", "scan 0", "randomkey", "dbsize", "script flush",
                 "function flush", "migrate 127.0.0.1 1 portal:x 0 10", "config get '*'", "get gw:x", "set gw:x 1",
                 "hgetall broker:x", "publish credential.invalidated x"]
    out = redis_as("portal-bff", forbidden)
    record("redis: portal-bff has no FLUSHALL/FLUSHDB/SWAPDB/KEYS/SCAN/SCRIPT/FUNCTION/MIGRATE/CONFIG, no gw:* or broker:*",
           len(out) == len(forbidden) and all("NOPERM" in line for line in out), "; ".join(out)[:200])
    out = redis_as("portal-bff", ["hset portal:m2 a 1", "hgetall portal:m2", "expire portal:m2 60", "del portal:m2"])
    record("redis: portal-bff keeps the Spring Session commands on portal:*", "NOPERM" not in " ".join(out) and "a" in out)
    out = redis_as("broker", ["eval 'return redis.call(\"set\", KEYS[1], 1)' 1 broker:m2", "eval 'return 1' 1 gw:m2",
                              "eval 'return redis.call(\"set\", \"gw:m2\", 1)' 1 broker:m2", "script flush", "function flush",
                              "del broker:m2"])
    record("redis: broker scripts only touch broker keys; no SCRIPT or FUNCTION", out[0] == "OK" and all(
        "NOPERM" in line or "ACL failure" in line for line in out[1:5]) and len(out) == 6, "; ".join(out)[:200])


def check_keycloak():
    def auth_request(redirect_uri):
        verifier, challenge = pkce()
        query = urllib.parse.urlencode({"client_id": "mcp-portal-bff", "response_type": "code", "scope": "openid",
                                        "redirect_uri": redirect_uri, "state": "s", "code_challenge": challenge,
                                        "code_challenge_method": "S256"})
        status, _, raw = http("GET", f"{mcpdev.OIDC}/auth?{query}")
        return status, b"kc-form-login" in raw

    record("keycloak: BFF callback under /api on the proxy address is accepted", auth_request(f"{PROXY}/api/v1/auth/callback") == (200, True))
    status, form = auth_request(f"{PROXY}/other/callback")
    record("keycloak: another path on the proxy address is refused", not form)
    status, form = auth_request("http://localhost:9/api/v1/auth/callback")
    record("keycloak: a foreign origin is refused", not form)


def check_proxy_live():
    status, _, _ = http("GET", f"{PROXY}/__proxy/health")
    if status != 200:
        skip("proxy on the running stack", f"nothing on {PROXY} (make up-m2)")
        return
    record("proxy: health", True)
    status, headers, _ = http("GET", f"{PROXY}/.well-known/jwks.json")
    record("proxy: gateway JWKS is not exposed", status == 404)
    status, _, _ = http("GET", f"{PROXY}/s/../admin/v1/registry/servers")
    record("proxy: admin API is not exposed", status in (400, 404))


class Portal:
    """A browser stand-in for the portal on the proxy address: cookie jar, manual redirects, XSRF header."""

    def __init__(self):
        self.jar = CookieJar(policy=mcpdev.LoopbackCookiePolicy())
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)

    def request(self, method, url, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = dict(headers or {})
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        if method not in ("GET", "HEAD"):
            hdrs.setdefault("X-XSRF-TOKEN", self.cookie("__Host-XSRF-TOKEN") or "")
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with self.opener.open(req, timeout=15) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def cookie(self, name):
        return next((c.value for c in self.jar if c.name == name), None)

    def follow(self, url, limit=10):
        """GET with redirects; stops at a page that is not a redirect and returns (status, headers, body, last url)."""
        for _ in range(limit):
            status, headers, raw = self.request("GET", url)
            if status in (301, 302, 303, 307) and headers.get("Location"):
                url = urllib.parse.urljoin(url, headers["Location"])
                continue
            return status, headers, raw, url
        raise mcpdev.Fail("too many redirects")

    def login(self, user):
        status, _, raw, url = self.follow(f"{PROXY}/api/v1/auth/login?returnPath=/")
        form = mcpdev.LoginFormParser()
        form.feed(raw.decode("utf-8", "replace"))
        if not form.action:
            raise mcpdev.Fail(f"no Keycloak login form (HTTP {status} at {url.split('?')[0]})")
        body = urllib.parse.urlencode({"username": user, "password": mcpdev.user_password(user), "credentialId": ""}).encode()
        req = urllib.request.Request(form.action, data=body, method="POST")
        status, headers, _ = self._raw(req)
        if status not in (302, 303):
            raise mcpdev.Fail(f"Keycloak login of {user}: HTTP {status}")
        return self.follow(urllib.parse.urljoin(form.action, headers["Location"]))

    def _raw(self, req):
        try:
            with self.opener.open(req, timeout=15) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()


def mcp_session(url, token, protocol="2025-11-25"):
    """(call, close): JSON-RPC over Streamable HTTP; event-stream answers are reduced to their last data line."""
    base = {**bearer(token), "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    state = {"id": 0, "session": None}

    def call(method, params=None, notify=False):
        state["id"] += 1
        message = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            message["id"] = state["id"]
        headers = dict(base)
        if state["session"]:
            headers["Mcp-Session-Id"] = state["session"]
            headers["MCP-Protocol-Version"] = protocol
        status, resp_headers, raw = http("POST", url, headers, json.dumps(message).encode())
        state["session"] = state["session"] or resp_headers.get("Mcp-Session-Id")
        text = raw.decode("utf-8", "replace")
        if "data:" in text and not text.lstrip().startswith("{"):
            text = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")][-1]
        return status, jbody(text.encode()) if text.strip() else {}

    return call


def tool_text(reply):
    result = reply.get("result", {})
    return " ".join(part.get("text", "") for part in result.get("content", [])) or json.dumps(reply.get("error", ""))


def check_portal():
    """The M2 demo through the single origin: OIDC login, catalog, PAT, OAuth connection, link, MCP client."""
    if http("GET", f"{PROXY}/api/v1/me")[0] != 401:
        skip("portal flow on the proxy address", "BFF does not answer 401 on /api/v1/me (make up-m2)")
        return
    portal = Portal()
    status, headers, raw, url = portal.login("alice")
    record("portal: OIDC login through the proxy ends on the portal origin", status == 200 and url.startswith(PROXY), f"HTTP {status} {url[:80]}")
    status, headers, raw = portal.request("GET", f"{PROXY}/api/v1/me")
    me = jbody(raw)
    record("portal: /me after login is alice", status == 200 and "alice" in json.dumps(me), f"HTTP {status} {raw[:80]!r}")
    record("portal: X-Request-Id on the BFF response", bool(headers.get("X-Request-Id")))
    record("portal: session and XSRF cookies are set", portal.cookie("__Host-session") is not None and portal.cookie("__Host-XSRF-TOKEN") is not None)
    status, _, raw = portal.request("GET", f"{PROXY}/api/v1/catalog")
    catalog = jbody(raw)
    servers = {item.get("system") or item.get("id"): item for item in catalog.get("servers", [])}
    record("portal: catalog lists the five systems", status == 200 and {"jira", "confluence", "gitlab", "servicedesk", "opensearch"} <= set(servers),
           f"HTTP {status} {sorted(servers)}")

    status, _, raw = portal.request("GET", f"{PROXY}/api/v1/connections")
    connected_before = {c.get("system") for c in jbody(raw) if isinstance(jbody(raw), list) and c.get("status") == "connected"} \
        if status == 200 and isinstance(jbody(raw), list) else set()
    status, _, raw = portal.request("PUT", f"{PROXY}/api/v1/connections/jira/secret", {"secret": "alice-pat"})
    record("portal: add a Jira PAT -> connected as alice", status == 200 and jbody(raw).get("backendUser") == "alice", f"HTTP {status} {raw[:100]!r}")
    status, _, raw = portal.request("PUT", f"{PROXY}/api/v1/connections/jira/secret", {"secret": "bob-pat"})
    record("portal: another user's PAT is accepted for the logged-in user (no identity comparison, contracts 2.2.3)",
           status == 200 and jbody(raw).get("backendUser") == "bob", f"HTTP {status} {raw[:100]!r}")
    portal.request("PUT", f"{PROXY}/api/v1/connections/jira/secret", {"secret": "alice-pat"})
    status, _, _ = http("PUT", f"{PROXY}/api/v1/connections/jira/secret", {"Content-Type": "application/json"}, b'{"secret":"alice-pat"}')
    record("portal: PUT without a session is refused (401 or 403)", status in (401, 403), f"HTTP {status}")
    status, _, _ = portal.request("PUT", f"{PROXY}/api/v1/connections/jira/secret", {"secret": "alice-pat"}, {"X-XSRF-TOKEN": "wrong"})
    record("portal: PUT with a wrong XSRF token -> 403", status == 403, f"HTTP {status}")

    status, _, raw = portal.request("POST", f"{PROXY}/api/v1/connections/gitlab/oauth", {"returnPath": "/catalog"})
    start = jbody(raw)
    record("portal: Connect through OAuth returns authorizeUrl", status == 200 and "authorizeUrl" in start, f"HTTP {status} {raw[:100]!r}")
    if status == 200:
        status, headers, _ = http("GET", start["authorizeUrl"] + "&mock_user=alice")
        callback = headers.get("Location", "")
        record("portal: the GitLab mock redirects to the callback on the proxy address", callback.startswith(CALLBACK), callback[:80])
        status, headers, _ = portal.request("GET", callback)
        landing = headers.get("Location", "")
        record("portal: OAuth callback finishes at the return path without an error", status in (302, 303) and "rror" not in landing
               and urllib.parse.urlparse(landing).path == "/catalog", f"HTTP {status} {landing[:80]}")
        status, _, raw = portal.request("GET", f"{PROXY}/api/v1/connections")
        listing = jbody(raw) if isinstance(jbody(raw), list) else []
        record("portal: GitLab shows as connected", any(c.get("system") == "gitlab" and c.get("status") == "connected" for c in listing), raw[:120].decode("utf-8", "replace"))
        status, _, raw = portal.request("POST", f"{PROXY}/api/v1/connections/gitlab/oauth", {"returnPath": "/catalog"})
        denied = http("GET", jbody(raw).get("authorizeUrl", "") + "&mock_action=deny")[1].get("Location", "")
        status, headers, _ = portal.request("GET", denied)
        record("portal: denying at the provider -> cancelled", "cancelled" in headers.get("Location", ""), headers.get("Location", "")[:100])

    slug = "m2-" + secrets.token_hex(3)
    status, _, raw = portal.request("POST", f"{PROXY}/api/v1/virtual-servers", {
        "slug": slug, "displayName": "m2 check", "tools": ["jira__get_issue", "jira__search_issues"]})
    created = jbody(raw)
    link = f"{PROXY}/v/{slug}/mcp"
    record("portal: create a link -> URL on the proxy origin", status == 201 and created.get("url") == link, f"HTTP {status} {raw[:120]!r}")
    status, _, raw = portal.request("POST", f"{PROXY}/api/v1/virtual-servers", {
        "slug": slug + "x", "displayName": "wide", "tools": ["jira__get_issue"], "resourceDefaults": {"projects": ["ABC"]}})
    record("portal: non-empty resourceDefaults -> 400 (CC-085)", status == 400, f"HTTP {status}")

    if status is not None:
        token_alice = mcpdev.login("alice", "claude-code")
        status, headers, _ = http("POST", link, {"Content-Type": "application/json"}, b"{}")
        challenge = headers.get("WWW-Authenticate", "")
        record("proxy: 401 challenge of the link points at the proxy origin", status == 401 and f'resource_metadata="{PROXY}/' in challenge, challenge[:140])
        status, _, raw = http("GET", f"{PROXY}/.well-known/oauth-protected-resource/v/{slug}/mcp")
        record("proxy: protected resource metadata of the link: resource == link URL",
               status == 200 and jbody(raw).get("resource") == link, raw[:120].decode("utf-8", "replace"))
        status, _, raw = http("GET", f"{PROXY}/.well-known/oauth-protected-resource/s/mcp-jira/mcp")
        record("proxy: protected resource metadata of mcp-jira names the proxy origin", status == 200 and jbody(raw).get("resource") == f"{PROXY}/s/mcp-jira/mcp")
        call = mcp_session(link, token_alice)
        status, reply = call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "m2", "version": "1"}})
        call("notifications/initialized", notify=True)
        status, reply = call("tools/list")
        names = sorted(t.get("name") for t in reply.get("result", {}).get("tools", []))
        record("MCP client: the link lists only its tools", names == ["jira__get_issue", "jira__search_issues"], str(names))
        status, reply = call("tools/call", {"name": "jira__get_issue", "arguments": {"issueKey": "ABC-1"}})
        record("MCP client: tools/call through the link reaches Jira as alice", "ABC-1" in tool_text(reply), tool_text(reply)[:120])
        status, reply = call("tools/call", {"name": "gitlab__get_project", "arguments": {"project": "team-a/app"}})
        record("MCP client: a tool outside the link is refused (tool_disabled)", "tool_disabled" in json.dumps(reply), json.dumps(reply)[:120])

        call = mcp_session(f"{PROXY}/s/mcp-gitlab/mcp", token_alice)
        call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "m2", "version": "1"}})
        call("notifications/initialized", notify=True)
        _, reply = call("tools/list")
        gitlab_tools = [t["name"] for t in reply.get("result", {}).get("tools", [])]
        record("MCP client: GitLab tools are listed", bool(gitlab_tools), str(reply)[:120])
        _, reply = call("tools/call", {"name": "gitlab__get_merge_request", "arguments": {"project": "team-a/app", "iid": 7}})
        record("MCP client: a GitLab tool works with the OAuth connection made in the portal", "result" in reply and "error" not in reply
               and not reply["result"].get("isError"), tool_text(reply)[:120])
        write_slug = slug + "w"
        status, _, raw = portal.request("POST", f"{PROXY}/api/v1/virtual-servers", {
            "slug": write_slug, "displayName": "m2 write", "tools": ["gitlab__comment_merge_request"]})
        write_link = f"{PROXY}/v/{write_slug}/mcp"
        record("portal: create a link with gitlab__comment_merge_request", status == 201 and jbody(raw).get("url") == write_link, f"HTTP {status} {raw[:120]!r}")
        write_call = mcp_session(write_link, token_alice)
        write_call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "m2", "version": "1"}})
        write_call("notifications/initialized", notify=True)
        note = "m2 note " + secrets.token_hex(3)
        _, reply = write_call("tools/call", {"name": "gitlab__comment_merge_request", "arguments": {"project": "team-a/app", "iid": 7, "body": note}})
        record("MCP client: gitlab__comment_merge_request through the link succeeds on the OAuth connection (scope api)",
               "result" in reply and "error" not in reply and not reply["result"].get("isError"), tool_text(reply)[:120])
        journal_status, _, journal_raw = http("GET", f"http://localhost:{PORTS['gitlab']}/__mock/journal", {"Authorization": "Basic " + base64.b64encode(
            f"admin:{cfg('MOCK_ADMIN_PASSWORD')}".encode()).decode()})
        notes = [row for row in (jbody(journal_raw) if journal_status == 200 else []) if isinstance(row, dict) and row.get("method") == "POST"
                 and "/merge_requests/7/notes" in row.get("path", "")]
        record("gitlab mock: the note on the MR was accepted (201) after the call through the link", bool(notes) and notes[-1].get("status") == 201,
               str(notes[-1] if notes else journal_status)[:120])
        status, _, _ = portal.request("DELETE", f"{PROXY}/api/v1/virtual-servers/{write_slug}")
        record("portal: delete the link with the write tool", status in (200, 204), f"HTTP {status}")

        status, _, _ = portal.request("DELETE", f"{PROXY}/api/v1/connections/gitlab")
        record("portal: disconnect GitLab", status in (200, 204), f"HTTP {status}")
        _, reply = call("tools/call", {"name": "gitlab__get_merge_request", "arguments": {"project": "team-a/app", "iid": 7}})
        text = json.dumps(reply)
        record("MCP client: after disconnecting, credential_required carries the portal link",
               "credential_required" in text and f"{PROXY}/connections?system=gitlab" in text, text[:160])
        if "gitlab" in connected_before:
            portal.request("POST", f"{PROXY}/api/v1/connections/gitlab/oauth", {"returnPath": "/catalog"})
        status, _, _ = portal.request("DELETE", f"{PROXY}/api/v1/virtual-servers/{slug}")
        record("portal: delete the link", status in (200, 204), f"HTTP {status}")
        status, _, _ = http("POST", link, {**bearer(token_alice), "Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                            b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
        record("proxy: a deleted link answers 404", status == 404, f"HTTP {status}")
    status, _, _ = portal.request("POST", f"{PROXY}/api/v1/auth/logout")
    record("portal: logout", status in (200, 204), f"HTTP {status}")
    status, _, _ = portal.request("GET", f"{PROXY}/api/v1/me")
    record("portal: /me after logout -> 401", status == 401)


def integration_skips():
    skip("web UI in a browser (rendering, clicks)", "no browser driver in this environment; the web build is served by the proxy, the API steps above run the same calls")
    skip("Claude Code with the link", "needs a person: claude mcp add + browser login (README, section Demo M1)")


def run():
    CLIENT["client_secret"] = cfg("GITLAB_OAUTH_CLIENT_SECRET")
    for part in (check_mocks, check_gitlab_oauth, check_broker_oauth, check_redis_acl, check_keycloak, check_proxy_live, check_portal):
        try:
            part()
        except (mcpdev.Fail, OSError, KeyError) as e:
            record(part.__name__, False, f"{type(e).__name__}: {e}")
    integration_skips()
    for status, name, detail in results:
        print(f"{status:4} {name}" + (f"  [{detail}]" if detail else ""))
    failed = sum(1 for r in results if r[0] == "FAIL")
    print(f"m2: {sum(1 for r in results if r[0] == 'PASS')} passed, {failed} failed, "
          f"{sum(1 for r in results if r[0] == 'SKIP')} skipped")
    return 1 if failed else 0


def mock_auth(name):
    if name not in PORTS:
        sys.exit(f"unknown mock {name}; one of {', '.join(PORTS)}")
    password = cfg("MOCK_ADMIN_PASSWORD")
    cred = base64.b64encode(f"admin:{password}".encode()).decode()
    status, _, raw = http("GET", f"http://localhost:{PORTS[name]}/__mock/journal", {"Authorization": f"Basic {cred}"})
    if status != 200:
        sys.exit(f"journal of {name}: HTTP {status}")
    for row in json.loads(raw):
        print(f"{row['status']:4} {row['method']:6} {row['path'][:60]:60} {row['auth']}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "run":
        sys.exit(run())
    if len(sys.argv) == 3 and sys.argv[1] == "mock-auth":
        sys.exit(mock_auth(sys.argv[2]))
    sys.exit(__doc__)
