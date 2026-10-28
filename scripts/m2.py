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
import sys
import urllib.error
import urllib.parse
import urllib.request

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
             "code_challenge": challenge, "code_challenge_method": "S256", "scope": "read_api", **extra}
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
    record("oauth: authorization code is single use", token(grant)[0] == 400)

    status, rotated = token({"grant_type": "refresh_token", "refresh_token": tokens.get("refresh_token", "")})
    record("oauth: refresh gives new tokens", status == 200 and rotated.get("access_token") != tokens.get("access_token"))
    record("oauth: the replaced access token stops working", whoami_gitlab(tokens.get("access_token", ""))[0] == 401)
    record("oauth: the replaced refresh token is invalid_grant", token({"grant_type": "refresh_token", "refresh_token": tokens.get("refresh_token", "")})[1].get("error") == "invalid_grant")

    verifier, challenge = pkce()
    _, _, params = authorize(challenge, "s1", mock_user="alice")
    status, body = token({**grant, "code": params["code"], "code_verifier": mcpdev.b64url(secrets.token_bytes(32))})
    record("oauth: wrong PKCE verifier -> invalid_grant", status == 400 and body.get("error") == "invalid_grant")

    _, _, params = authorize(challenge, "s2", mock_user="bob")
    status, body = token({**grant, "code": params["code"], "code_verifier": verifier})
    record("oauth: authorizing as bob gives a token whose whoami is bob (broker must report identity_mismatch for alice)",
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
    record("broker: another GitLab account -> 422 identity_mismatch", status == 422 and done.get("error") == "identity_mismatch", f"HTTP {status} {done}")

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


def integration_skips():
    for name, missing in (
        ("portal: OIDC login at the proxy address", "BFF M2 (callback under /api), web M2"),
        ("portal: catalog with statuses", "BFF M2 + gateway M2 admin API"),
        ("portal: Connect through OAuth (GitLab mock)", "BFF oauth callback and start (ws-07), web (ws-08); broker side is checked above"),
        ("portal: Add PAT (Jira) and identity_mismatch", "BFF M2, web M2"),
        ("portal: create a virtual server link", "gateway M2 admin API (vs), BFF, web"),
        ("Claude Code: virtual server link works through the proxy", "gateway M2 (publicBaseUrl, /v/), proxy in front"),
        ("Claude Code: credential_required carries the portal link", "gateway + MCP server connectUrl on the proxy address"),
    ):
        skip(name, "waiting for " + missing)


def run():
    CLIENT["client_secret"] = cfg("GITLAB_OAUTH_CLIENT_SECRET")
    for part in (check_mocks, check_gitlab_oauth, check_broker_oauth, check_keycloak, check_proxy_live):
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
