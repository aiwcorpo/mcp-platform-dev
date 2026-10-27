#!/usr/bin/env python3
"""Smoke test of the running environment (profile infra). Exit code 1 if any check fails.
Secrets are handed to processes inside containers through stdin, never through argv."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcpdev  # noqa: E402

ROOT = mcpdev.ROOT
COMPOSE = [str(ROOT / "scripts" / "compose.sh")]
PROJECT = mcpdev.cfg("COMPOSE_PROJECT_NAME", "mcp-dev")
OPENSSL_IMAGE = "alpine/openssl:3.5.9@sha256:1c361a204e9791bec6202bccbd1065eafaa65cb5a1a9a06ae000238569fba6a2"
CURL_IMAGE = "wiremock/wiremock:3.13.2@sha256:0d4ecb3e4dc8213fd7a4d37d6a78f6e6b553a6d2e15bd51b0999781282ac61b3"
JIRA = f"http://localhost:{mcpdev.cfg('JIRA_MOCK_PORT', '18081')}"
BROKER_TLS_NAME = mcpdev.cfg("MCP_BROKER_TLS_NAME", "mcp-credential-broker.internal")
GATEWAY_TLS_NAME = mcpdev.cfg("MCP_GATEWAY_INTERNAL_TLS_NAME", "mcp-gateway.internal")
COMPONENT_AUDIENCES = {"mcp-gateway", "mcp-credential-broker", "mcp-gateway-admin", "opensearch"}
failures = []


def check(name, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}  {name}{'  (' + detail + ')' if detail and not condition else ''}")
    if not condition:
        failures.append(name)


def run(*args, stdin=None):
    return subprocess.run(list(args), input=stdin, capture_output=True, text=True)


def compose_exec(service, script, stdin=""):
    return run(*COMPOSE, "exec", "-T", service, "sh", "-c", script, stdin=stdin)


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def expect_fail(fn):
    try:
        fn()
        return False
    except mcpdev.Fail:
        return True


def audiences(claims):
    aud = claims.get("aud")
    return set(aud) if isinstance(aud, list) else ({aud} if aud else set())


print("== compose and contracts")
full = run(*COMPOSE, "--profile", "full", "config", "--quiet")
check("profile full renders", full.returncode == 0, full.stderr[-200:])
ref = mcpdev.cfg("MCP_CONTRACTS_REF")
ref_file = ROOT / "out" / "contracts" / "REF"
check(f"stubs extracted from mcp-contracts {ref}",
      ref_file.exists() and ref_file.read_text().split()[0] == ref, ref_file.read_text() if ref_file.exists() else "missing")
mounts = run("docker", "inspect", "--format", "{{range .Mounts}}{{.Source}} {{end}}", f"{PROJECT}-broker-mock-1")
check("broker stub mounts out/contracts, not the mcp-contracts working copy",
      str(ROOT / "out" / "contracts") in mounts.stdout and "mcp-contracts" not in mounts.stdout, mounts.stdout)
check("broker stub does not mount CA key or other identities' keys",
      "ca.key" not in mounts.stdout and "mcp-gateway/tls.key" not in mounts.stdout, mounts.stdout)

print("== containers")
ps = run(*COMPOSE, "--profile", "infra", "ps", "--all", "--format", "json")
services = {}
for line in ps.stdout.splitlines():
    if line.strip():
        row = json.loads(line)
        services[row["Service"]] = row
for svc in ("keycloak", "openbao", "redis", "postgres", "jira-mock", "broker-mock", "gateway-admin-mock"):
    row = services.get(svc, {})
    check(f"{svc} healthy", row.get("Health") == "healthy", row.get("Status", "missing"))
init = services.get("openbao-init", {})
check("openbao-init completed", init.get("State") == "exited" and init.get("ExitCode") == 0, init.get("Status", "missing"))

print("== Keycloak realm corp (C1, C9)")
issuer = f"{mcpdev.KEYCLOAK_URL}/realms/{mcpdev.REALM}"
alice_token = mcpdev.login("alice")
alice = mcpdev.decode_jwt(alice_token)
check("alice: aud == mcp-gateway", alice.get("aud") == "mcp-gateway", str(alice.get("aud")))
check("alice: azp == claude-code", alice.get("azp") == "claude-code")
check("alice: iss is the fixed frontend URL", alice.get("iss") == issuer, alice.get("iss"))
check("alice: required C1 claims present",
      all(k in alice for k in ("iss", "sub", "aud", "exp", "iat", "azp", "preferred_username", "groups")))
groups = alice.get("groups", [])
check("alice: groups without path (CC-007)", groups and not any(g.startswith("/") for g in groups), str(groups))
check("alice: jira read/write + proj-ABC",
      {"mcp-jira-read", "mcp-jira-write", "mcp-jira-proj-ABC"} <= set(groups), str(groups))
check("alice: token lifetime <= 60 min", alice["exp"] - alice["iat"] <= 3600)
bob = mcpdev.decode_jwt(mcpdev.login("bob"))
check("bob: jira read only", "mcp-jira-read" in bob["groups"] and "mcp-jira-write" not in bob["groups"])
carol = mcpdev.decode_jwt(mcpdev.login("carol"))
check("carol: mcp-platform-admin", "mcp-platform-admin" in carol["groups"])
mallory = mcpdev.decode_jwt(mcpdev.login("mallory"))
check("mallory: no groups", not mallory.get("groups"))

check("claude-code cannot ask for the broker audience scope",
      expect_fail(lambda: mcpdev.login("alice", scope="openid mcp-credential-broker-audience")))
check("wrong password rejected", expect_fail(lambda: mcpdev.login("alice", password="wrong")))

try:
    urllib.request.urlopen(urllib.request.Request(
        f"{issuer}/clients-registrations/openid-connect", method="POST",
        data=json.dumps({"redirect_uris": ["http://localhost:1/cb"], "token_endpoint_auth_method": "none"}).encode(),
        headers={"Content-Type": "application/json"}))
    dcr_status = 201
except urllib.error.HTTPError as e:
    dcr_status = e.code
check("anonymous dynamic client registration rejected (explicit Trusted Hosts policy)", dcr_status == 403,
      str(dcr_status))

print("== token exchange (C4)")
gw_token = mcpdev.exchange(alice_token, "mcp-gateway", "mcp-credential-broker")
gw = mcpdev.decode_jwt(gw_token)
check("gateway: alice -> aud=mcp-credential-broker", gw.get("aud") == "mcp-credential-broker", str(gw.get("aud")))
check("gateway: exchanged token keeps sub", gw.get("sub") == alice["sub"])
bff = mcpdev.decode_jwt(mcpdev.exchange(mcpdev.login("alice", "mcp-portal-bff"), "mcp-portal-bff", "mcp-credential-broker"))
check("bff: alice -> aud=mcp-credential-broker, azp=mcp-portal-bff",
      bff.get("aud") == "mcp-credential-broker" and bff.get("azp") == "mcp-portal-bff")
os_token = mcpdev.decode_jwt(mcpdev.exchange(gw_token, "mcp-credential-broker", "opensearch"))
check("broker: subjectToken (aud=broker) -> aud=opensearch", os_token.get("aud") == "opensearch"
      and os_token.get("sub") == alice["sub"], str(os_token.get("aud")))
check("gateway cannot exchange to opensearch", expect_fail(lambda: mcpdev.exchange(alice_token, "mcp-gateway", "opensearch")))
check("gateway cannot exchange to mcp-gateway-admin",
      expect_fail(lambda: mcpdev.exchange(alice_token, "mcp-gateway", "mcp-gateway-admin")))
check("gateway cannot turn a broker token into aud=opensearch",
      expect_fail(lambda: mcpdev.exchange(gw_token, "mcp-gateway", "opensearch")))
check("bff cannot exchange a token issued to the gateway",
      expect_fail(lambda: mcpdev.exchange(gw_token, "mcp-portal-bff", "mcp-credential-broker")))
check("claude-code (public) cannot exchange",
      expect_fail(lambda: mcpdev.exchange(alice_token, "claude-code", "mcp-credential-broker")))
check("bff cannot exchange the claude-code token",
      expect_fail(lambda: mcpdev.exchange(alice_token, "mcp-portal-bff", "mcp-credential-broker")))
try:
    no_aud = mcpdev.decode_jwt(mcpdev.exchange(alice_token, "mcp-gateway"))
    check("gateway exchange without audience yields no component audience", not audiences(no_aud) & COMPONENT_AUDIENCES,
          str(no_aud.get("aud")))
except mcpdev.Fail:
    check("gateway exchange without audience yields no component audience", True)

# A component inside the network talks to http://keycloak:8080; `iss` must stay the same.
form = urllib.parse.urlencode({
    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "client_id": "mcp-gateway",
    "client_secret": mcpdev.cfg("KC_CLIENT_MCP_GATEWAY_SECRET"), "subject_token": alice_token,
    "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
    "audience": "mcp-credential-broker", "scope": "mcp-credential-broker-audience"})
inside = compose_exec("jira-mock", "curl -s --data-binary @- http://keycloak:8080/realms/corp/protocol/openid-connect/token",
                      stdin=form)
try:
    inside_claims = mcpdev.decode_jwt(json.loads(inside.stdout)["access_token"])
    check("exchange from inside the network: same iss", inside_claims.get("iss") == issuer, inside_claims.get("iss"))
except (ValueError, KeyError) as e:
    check("exchange from inside the network: same iss", False, f"{e}: {inside.stdout[:200]}")

print("== Jira mock (C4.1 whoami, invariant 2)")
for pat, expected in (("alice-pat", "alice"), ("bob-pat", "bob"), ("someone-elses-pat", "someone-else")):
    status, body = http_get(f"{JIRA}/rest/api/2/myself", {"Authorization": f"Bearer {pat}"})
    name = json.loads(body).get("name") if status == 200 else None
    check(f"whoami {pat} -> {expected}", name == expected, f"{status} {name}")
status, _ = http_get(f"{JIRA}/rest/api/2/myself")
check("whoami without credentials -> 401", status == 401)
status, body = http_get(f"{JIRA}/rest/api/2/issue/ABC-1", {"Authorization": "Bearer alice-pat"})
check("issue ABC-1 readable with alice-pat", status == 200 and json.loads(body)["key"] == "ABC-1")
status, body = http_get(f"{JIRA}/rest/api/2/project/HR", {"Authorization": "Bearer alice-pat"})
check("project HR has category Restricted",
      status == 200 and json.loads(body).get("projectCategory", {}).get("name") == "Restricted")
status, _ = http_get(f"{JIRA}/rest/api/2/issue/ABC-1", {"Authorization": f"Bearer {alice_token}"})
check("data with an IdP token (passthrough) -> 401", status == 401, str(status))
status, _ = http_get(f"{JIRA}/rest/api/2/search?jql=project%3DABC", {"Authorization": "Bearer someone-elses-pat"})
check("data with an unknown PAT -> 401", status == 401, str(status))
rows = mcpdev.jira_auth()
check("journal shows which credential reached Jira (PAT vs passthrough)",
      any(r["credential"] == "PAT alice-pat" for r in rows) and any("passthrough" in r["credential"] for r in rows))

print("== broker stub over mTLS (C4 PUT /v1/connections/{system}/secret)")
status, body = mcpdev.put_secret("alice", "jira", "alice-pat")
check(f"put-secret alice jira -> 200 connected (TLS name {BROKER_TLS_NAME} verified)",
      status == 200 and body.get("status") == "connected", f"{status} {body}")
check("put-secret rejects a broker certificate with another name",
      expect_fail(lambda: mcpdev.put_secret("alice", "jira", "alice-pat", tls_name="mcp-jira.internal")))
check("put-secret with untrusted client cert rejected",
      expect_fail(lambda: mcpdev.put_secret("alice", "jira", "alice-pat", identity="untrusted/mcp-gateway")))
broker = f"https://localhost:{mcpdev.cfg('BROKER_MOCK_PORT', '18443')}"
nocert = run("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--cacert", str(ROOT / "certs/ca/ca.crt"),
             f"{broker}/v1/systems")
check("broker stub without client cert rejected", nocert.stdout == "000", nocert.stdout)
journal = run("curl", "-s", "--cacert", str(ROOT / "certs/ca/ca.crt"), "--cert", str(ROOT / "certs/mcp-portal-bff/tls.crt"),
              "--key", str(ROOT / "certs/mcp-portal-bff/tls.key"), f"{broker}/__admin/requests")
check("broker stub keeps no request journal (PATs)", "alice-pat" not in journal.stdout, journal.stdout[:200])

print("== OpenBao (KV v2, Transit, AppRole, audit)")
login_out = compose_exec(
    "openbao", f"bao write -field=token auth/approle/login role_id={mcpdev.cfg('BAO_BROKER_ROLE_ID')} secret_id=-",
    stdin=mcpdev.cfg("BAO_BROKER_SECRET_ID"))
broker_token = login_out.stdout.strip()
check("approle mcp-credential-broker login", login_out.returncode == 0 and broker_token, login_out.stderr[-200:])
as_broker = "read -r BAO_TOKEN; export BAO_TOKEN; "
enc = compose_exec("openbao", as_broker + "bao write -field=ciphertext transit/encrypt/mcp-credential-broker plaintext=dGVzdA==",
                   stdin=broker_token + "\n")
check("broker token can use transit key", enc.stdout.startswith("vault:v1:"), enc.stderr[-200:])
kv = compose_exec("openbao", as_broker + "bao kv put -mount=secret mcp/connections/smoke/jira probe=1 >/dev/null && "
                  "bao kv delete -mount=secret mcp/connections/smoke/jira >/dev/null", stdin=broker_token + "\n")
check("broker token can write KV v2 connections path", kv.returncode == 0, kv.stderr[-200:])
deny = compose_exec("openbao", as_broker + "bao kv get -mount=secret other/path", stdin=broker_token + "\n")
check("broker token denied outside its path", deny.returncode != 0 and "permission denied" in deny.stderr)
audit_list = compose_exec("openbao", as_broker + "bao audit list", stdin=mcpdev.cfg("BAO_ROOT_TOKEN") + "\n")
check("audit device enabled", "stdout/" in audit_list.stdout, audit_list.stderr[-200:])

print("== Redis ACL per component")


def redis_as(user, *commands):
    script = "read -r REDISCLI_AUTH; export REDISCLI_AUTH; " + "; ".join(
        f"redis-cli --no-auth-warning --user {user} {c}" for c in commands)
    pw = mcpdev.cfg(f"REDIS_{user.upper().replace('-', '_')}_PASSWORD")
    return [line for line in compose_exec("redis", script, stdin=pw + "\n").stdout.splitlines() if line.strip()]


noauth = compose_exec("redis", "redis-cli ping")
check("default user disabled (NOAUTH)", "NOAUTH" in noauth.stdout + noauth.stderr, noauth.stdout)
out = redis_as("gateway", "set gw:smoke 1", "set broker:smoke 1", "publish credential.invalidated x", "flushall")
check("gateway: own prefix gw:*", out[0] == "OK", str(out))
check("gateway: no broker keys, no publish, no flushall", all("NOPERM" in o for o in out[1:]), str(out))
out = redis_as("broker", "set broker:smoke 1", "set jti:mcp-credential-broker:smoke 1",
               "publish credential.invalidated x", "set gw:smoke 1", "publish other.channel x")
check("broker: broker:*, jti:mcp-credential-broker:*, publish credential.invalidated",
      out[:2] == ["OK", "OK"] and out[2].isdigit(), str(out))
check("broker: no gateway keys, no other channels", all("NOPERM" in o for o in out[3:]), str(out))
out = redis_as("mcp-jira", "set jti:mcp-jira:smoke 1 EX 60", "set jti:mcp-confluence:smoke 1", "publish credential.invalidated x")
check("mcp-jira: only jti:mcp-jira:*", out[0] == "OK" and all("NOPERM" in o for o in out[1:]), str(out))

print("== PostgreSQL")
for user, db, ok in (("broker", "broker", True), ("gateway", "gateway", True), ("broker", "gateway", False)):
    res = compose_exec("postgres", f"read -r PGPASSWORD; export PGPASSWORD; psql -h 127.0.0.1 -U {user} -d {db} -tAc 'select 1'",
                       stdin=mcpdev.cfg(f"{user.upper()}_DB_PASSWORD") + "\n")
    check(f"postgres {user} -> db {db} {'allowed' if ok else 'denied'}", (res.stdout.strip() == "1") == ok,
          res.stderr[-200:])

print("== networks")
for target, reachable in (("http://jira.internal:8080/__admin/health", True), ("http://redis:6379", True),
                          ("http://openbao:8200/v1/sys/health", False), ("http://keycloak:8080/", False),
                          ("http://postgres:5432", False), (f"https://{BROKER_TLS_NAME}:8443/", False)):
    res = run("docker", "run", "--rm", "--network", f"{PROJECT}-servers", "--entrypoint", "curl", CURL_IMAGE,
              "-s", "-o", "/dev/null", "-m", "3", target)
    # curl exit 6 = name not resolved; 52 = empty reply (Redis/PostgreSQL speak no HTTP but are reachable).
    resolved = res.returncode != 6
    check(f"servers network: {target.split('/')[2]} {'reachable' if reachable else 'not visible'}",
          resolved == reachable, f"curl exit {res.returncode}")

print("== certificates (C3, C4, C5, CC-035)")
certs = ROOT / "certs"
expectations = {
    "mcp-jira": ("mcp-jira.internal", "TLS Web Server Authentication", "TLS Web Client Authentication"),
    "mcp-gateway": (GATEWAY_TLS_NAME, "TLS Web Server Authentication, TLS Web Client Authentication", None),
    "mcp-credential-broker": (BROKER_TLS_NAME, "TLS Web Server Authentication", "TLS Web Client Authentication"),
    "mcp-portal-bff": (None, "TLS Web Client Authentication", "TLS Web Server Authentication"),
}
for name, (tls_name, eku, forbidden_eku) in expectations.items():
    # Same pinned openssl as gen-certs.sh; the host may only have LibreSSL.
    out = run("docker", "run", "--rm", "-v", f"{certs}:/certs:ro", "--entrypoint", "/bin/sh", OPENSSL_IMAGE, "-c",
              f"openssl x509 -in /certs/{name}/tls.crt -noout -subject -ext subjectAltName,extendedKeyUsage && "
              f"openssl verify -CAfile /certs/ca/ca.crt /certs/{name}/tls.crt")
    text = out.stdout.replace(" = ", "=")
    check(f"{name}: CN={name}" + (f", SAN {tls_name}" if tls_name else ", no localhost SAN"),
          f"CN={name}" in text and (f"DNS:{tls_name}" in text if tls_name else "localhost" not in text), out.stdout)
    check(f"{name}: EKU {eku}" + (f", without {forbidden_eku.split()[2]}" if forbidden_eku else ""),
          eku in text and (forbidden_eku is None or forbidden_eku not in text), out.stdout)
    check(f"{name}: signed by dev CA", out.returncode == 0 and f"{name}/tls.crt: OK" in out.stdout)

print("== audit tool (make audit, C8)")
vectors = ROOT / "out" / "contracts" / "test-vectors" / "audit-events"
rid = "0192d1b2-0000-7000-8000-00000000a0d1"
base = next(json.loads(p.read_text())["event"] for p in sorted(vectors.glob("*.json"))
            if json.loads(p.read_text()).get("expected", {}).get("valid"))
lines = [f"{component}-1  | " + json.dumps({**base, "requestId": rid, "component": component, "componentId": cid})
         for component, cid in mcpdev.AUDIT_COMPONENTS.items()]
lines += ["unrelated log line " + rid, json.dumps({**base, "requestId": "0192d1b2-0000-7000-8000-0000000000ff"})]


def audit_exit(log_lines):
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write("\n".join(log_lines))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return mcpdev.audit(rid, f.name)
    finally:
        Path(f.name).unlink()


check("audit: events of one requestId found in all three components",
      {e["component"] for e in mcpdev.audit_events(lines, rid)} == set(mcpdev.AUDIT_COMPONENTS))
check("audit: exit 0 with gateway, broker and server", audit_exit(lines) == 0)
check("audit: exit 1 when the server event is missing", audit_exit(lines[:2]) == 1)

print()
if failures:
    print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
