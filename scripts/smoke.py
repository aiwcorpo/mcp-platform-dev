#!/usr/bin/env python3
"""Smoke test of the running environment (profile infra). Exit code 1 if any check fails."""

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcpdev  # noqa: E402

ROOT = mcpdev.ROOT
COMPOSE = [str(ROOT / "scripts" / "compose.sh")]
OPENSSL_IMAGE = "alpine/openssl:3.5.9"
JIRA = f"http://localhost:{mcpdev.cfg('JIRA_MOCK_PORT', '18081')}"
failures = []


def check(name, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}  {name}{'  (' + detail + ')' if detail and not condition else ''}")
    if not condition:
        failures.append(name)


def run(*args, stdin=None):
    return subprocess.run(list(args), input=stdin, capture_output=True, text=True)


def compose_exec(service, *cmd, stdin=None):
    return run(*COMPOSE, "exec", "-T", service, *cmd, stdin=stdin)


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


print("== compose")
full = run(*COMPOSE, "--profile", "full", "config", "--quiet")
check("profile full renders", full.returncode == 0, full.stderr[-200:])

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
alice = mcpdev.decode_jwt(mcpdev.login("alice"))
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
check("anonymous dynamic client registration rejected", dcr_status == 403, str(dcr_status))

print("== token exchange (C4)")
gw = mcpdev.decode_jwt(mcpdev.exchange(mcpdev.login("alice"), "mcp-gateway", "mcp-credential-broker"))
check("gateway: alice -> aud=mcp-credential-broker", gw.get("aud") == "mcp-credential-broker", str(gw.get("aud")))
check("gateway: exchanged token keeps sub", gw.get("sub") == alice["sub"])
bff = mcpdev.decode_jwt(mcpdev.exchange(mcpdev.login("alice", "mcp-portal-bff"), "mcp-portal-bff", "mcp-credential-broker"))
check("bff: alice -> aud=mcp-credential-broker, azp=mcp-portal-bff",
      bff.get("aud") == "mcp-credential-broker" and bff.get("azp") == "mcp-portal-bff")
check("gateway cannot exchange to mcp-gateway-admin",
      expect_fail(lambda: mcpdev.exchange(mcpdev.login("alice"), "mcp-gateway", "mcp-gateway-admin")))

# A component inside the network talks to http://keycloak:8080; `iss` must stay the same.
subject = mcpdev.login("alice")
inside = compose_exec(
    "jira-mock", "curl", "-s", "http://keycloak:8080/realms/corp/protocol/openid-connect/token",
    "-d", "grant_type=urn:ietf:params:oauth:grant-type:token-exchange",
    "-d", "client_id=mcp-gateway", "-d", f"client_secret={mcpdev.cfg('KC_CLIENT_MCP_GATEWAY_SECRET')}",
    "-d", "subject_token_type=urn:ietf:params:oauth:token-type:access_token",
    "-d", "audience=mcp-credential-broker", "-d", "scope=mcp-credential-broker-audience",
    "-d", f"subject_token={subject}")
try:
    inside_claims = mcpdev.decode_jwt(json.loads(inside.stdout)["access_token"])
    check("exchange from inside the network: same iss", inside_claims.get("iss") == issuer, inside_claims.get("iss"))
except (ValueError, KeyError) as e:
    check("exchange from inside the network: same iss", False, f"{e}: {inside.stdout[:200]}")

print("== Jira mock (C4.1 whoami)")
for pat, expected in (("alice-pat", "alice"), ("bob-pat", "bob"), ("someone-elses-pat", "someone-else")):
    status, body = http_get(f"{JIRA}/rest/api/2/myself", {"Authorization": f"Bearer {pat}"})
    name = json.loads(body).get("name") if status == 200 else None
    check(f"whoami {pat} -> {expected}", name == expected, f"{status} {name}")
status, _ = http_get(f"{JIRA}/rest/api/2/myself")
check("whoami without credentials -> 401", status == 401)
status, body = http_get(f"{JIRA}/rest/api/2/issue/ABC-1", {"Authorization": "Bearer alice-pat"})
check("issue ABC-1 readable", status == 200 and json.loads(body)["key"] == "ABC-1")
status, body = http_get(f"{JIRA}/rest/api/2/project/HR", {"Authorization": "Bearer alice-pat"})
check("project HR has category Restricted",
      status == 200 and json.loads(body).get("projectCategory", {}).get("name") == "Restricted")

print("== broker stub over mTLS (C4 PUT /v1/connections/{system}/secret)")
status, body = mcpdev.put_secret("alice", "jira", "alice-pat")
check("put-secret alice jira -> 200 connected", status == 200 and body.get("status") == "connected", f"{status} {body}")
check("put-secret with untrusted client cert rejected",
      expect_fail(lambda: mcpdev.put_secret("alice", "jira", "alice-pat", identity="untrusted/mcp-gateway")))
broker = f"https://localhost:{mcpdev.cfg('BROKER_MOCK_PORT', '18443')}/v1/systems"
nocert = run("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--cacert", str(ROOT / "certs/ca/ca.crt"), broker)
check("broker stub without client cert rejected", nocert.stdout == "000", nocert.stdout)

print("== OpenBao (KV v2, Transit, AppRole)")
login_out = compose_exec("openbao", "sh", "-c",
                         "bao write -field=token auth/approle/login role_id=\"$0\" secret_id=\"$1\"",
                         mcpdev.cfg("BAO_BROKER_ROLE_ID"), mcpdev.cfg("BAO_BROKER_SECRET_ID"))
broker_token = login_out.stdout.strip()
check("approle mcp-credential-broker login", login_out.returncode == 0 and broker_token, login_out.stderr[-200:])
enc = compose_exec("openbao", "sh", "-c",
                   "BAO_TOKEN=\"$0\" bao write -field=ciphertext transit/encrypt/mcp-credential-broker plaintext=dGVzdA==",
                   broker_token)
check("broker token can use transit key", enc.stdout.startswith("vault:v1:"), enc.stderr[-200:])
kv = compose_exec("openbao", "sh", "-c",
                  "BAO_TOKEN=\"$0\" bao kv put -mount=secret mcp/connections/smoke/jira probe=1 >/dev/null && "
                  "BAO_TOKEN=\"$0\" bao kv delete -mount=secret mcp/connections/smoke/jira >/dev/null", broker_token)
check("broker token can write KV v2 connections path", kv.returncode == 0, kv.stderr[-200:])
deny = compose_exec("openbao", "sh", "-c", "BAO_TOKEN=\"$0\" bao kv get -mount=secret other/path", broker_token)
check("broker token denied outside its path", deny.returncode != 0 and "permission denied" in deny.stderr)

print("== Redis, PostgreSQL")
pong = compose_exec("redis", "sh", "-c", "REDISCLI_AUTH=\"$REDIS_PASSWORD\" redis-cli ping")
check("redis ping with password", pong.stdout.strip() == "PONG")
noauth = compose_exec("redis", "redis-cli", "ping")
check("redis without password rejected", "NOAUTH" in noauth.stdout + noauth.stderr)
for user, db, ok in (("broker", "broker", True), ("gateway", "gateway", True), ("broker", "gateway", False)):
    pw = mcpdev.cfg(f"{user.upper()}_DB_PASSWORD")
    res = compose_exec("postgres", "sh", "-c", "PGPASSWORD=\"$0\" psql -h 127.0.0.1 -U \"$1\" -d \"$2\" -tAc 'select 1'",
                       pw, user, db)
    check(f"postgres {user} -> db {db} {'allowed' if ok else 'denied'}", (res.stdout.strip() == "1") == ok,
          res.stderr[-200:])

print("== certificates (C3, C4, C5)")
certs = ROOT / "certs"
for name in ("mcp-jira", "mcp-gateway", "mcp-credential-broker", "mcp-portal-bff"):
    # Same pinned openssl as gen-certs.sh; the host may only have LibreSSL.
    out = run("docker", "run", "--rm", "-v", f"{certs}:/certs:ro", "--entrypoint", "/bin/sh", OPENSSL_IMAGE, "-c",
              f"openssl x509 -in /certs/{name}/tls.crt -noout -subject -ext subjectAltName && "
              f"openssl verify -CAfile /certs/ca/ca.crt /certs/{name}/tls.crt")
    check(f"{name}: CN={name}, SAN {name}.internal",
          f"CN={name}" in out.stdout.replace(" = ", "=") and f"DNS:{name}.internal" in out.stdout, out.stdout)
    check(f"{name}: signed by dev CA", out.returncode == 0 and f"{name}/tls.crt: OK" in out.stdout)

print()
if failures:
    print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
