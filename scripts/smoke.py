#!/usr/bin/env python3
"""Smoke test of the running environment. Exit code 1 if any check fails.

  smoke.py            READ ONLY: nothing is written to OpenBao, Redis, the mocks or the broker, no shared test user (alice, bob, carol,
                      mallory) logs in and no wrong password is tried; token checks use one throw-away Keycloak user that is deleted
                      afterwards (Keycloak itself gets that user, and the broker and gateway get a purge for its id).
  smoke.py --write    adds the checks that write (Redis keys with a TTL, an OpenBao KV path and Transit call, a mapping of the Jira mock,
                      a wrong password of the throw-away user). Needs CONFIRM=1 in the environment (make smoke-write CONFIRM=1).
                      Never publishes on the live channel credential.invalidated.
Secrets are handed to processes inside containers through stdin, never through argv."""

import argparse
import base64
import contextlib
import io
import json
import os
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
arguments = argparse.ArgumentParser()
arguments.add_argument("--write", action="store_true")
WRITE = arguments.parse_args().write
if WRITE and os.environ.get("CONFIRM") != "1":
    sys.exit("smoke: --write changes the running stack (short-lived test keys, one OpenBao path, one Jira mock mapping); run make smoke-write CONFIRM=1")


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

print("== containers")
ps = run(*COMPOSE, "--profile", "infra", "--profile", "full", "--profile", "m2", "--profile", "ci", "ps", "--all", "--format", "json")
services = {}
for line in ps.stdout.splitlines():
    if line.strip():
        row = json.loads(line)
        services[row["Service"]] = row
# Either the C4/C5 stubs (make up) or the real broker and gateway (make up-full and later layers) answer on the broker side;
# the stubs share the aliases of the real components, so they never run together.
stub_mode = services.get("broker-mock", {}).get("State") == "running"
print(f"   mode: {'contract stubs' if stub_mode else 'real broker and gateway'}")
if stub_mode:
    mounts = run("docker", "inspect", "--format", "{{range .Mounts}}{{.Source}} {{end}}", f"{PROJECT}-broker-mock-1")
    check("broker stub mounts out/contracts, not the mcp-contracts working copy",
          str(ROOT / "out" / "contracts") in mounts.stdout and "mcp-contracts" not in mounts.stdout, mounts.stdout)
    check("broker stub does not mount CA key or other identities' keys",
          "ca.key" not in mounts.stdout and "mcp-gateway/tls.key" not in mounts.stdout, mounts.stdout)
component_services = ("broker-mock", "gateway-admin-mock") if stub_mode else ("mcp-credential-broker", "mcp-gateway")
for svc in ("keycloak", "openbao", "redis", "postgres", "jira-mock") + component_services:
    row = services.get(svc, {})
    check(f"{svc} healthy", row.get("Health") == "healthy", row.get("Status", "missing"))
# wait-init.sh removes the finished job after make up, so a missing container is the normal state; the read of the AppRole below proves its result.
init = services.get("openbao-init")
check("openbao-init did not fail", init is None or (init.get("State") == "exited" and init.get("ExitCode") == 0),
      init.get("Status", "") if init else "")

def tls_refused(result):
    """curl exit 35 or 56: the handshake was refused or cut. 7 (port closed), 58 and 60 are other failures and no proof of anything."""
    return result.returncode in (35, 56)


def curl_status(*args):
    done = run("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", *args)
    return done


print("== throw-away user for the token checks")
with mcpdev.TempUser(["mcp-client-claude-code", "mcp-jira-read", "mcp-jira-write", "mcp-jira-proj-ABC", "mcp-opensearch-read"], otp=False) as probe:
    print("== Keycloak realm corp (C1, C9)")
    issuer = f"{mcpdev.KEYCLOAK_URL}/realms/{mcpdev.REALM}"
    user_token = mcpdev.login(probe.name)
    claims = mcpdev.decode_jwt(user_token)
    check("probe user: aud == mcp-gateway", claims.get("aud") == "mcp-gateway", str(claims.get("aud")))
    check("probe user: azp == claude-code", claims.get("azp") == "claude-code")
    check("probe user: iss is the fixed frontend URL", claims.get("iss") == issuer, claims.get("iss"))
    check("probe user: required C1 claims present",
          all(k in claims for k in ("iss", "sub", "aud", "exp", "iat", "azp", "preferred_username", "groups")))
    groups = claims.get("groups", [])
    check("probe user: groups without path (CC-007)", groups and not any(g.startswith("/") for g in groups), str(groups))
    check("probe user: jira read/write + proj-ABC", {"mcp-jira-read", "mcp-jira-write", "mcp-jira-proj-ABC"} <= set(groups), str(groups))
    check("probe user: token lifetime <= 60 min", claims["exp"] - claims["iat"] <= 3600)


    def realm_groups(login_name):
        status, found = mcpdev.kc_admin("GET", f"/users?username={login_name}&exact=true")
        if status != 200 or not found:
            return None
        return {g["name"] for g in mcpdev.kc_admin("GET", f"/users/{found[0]['id']}/groups")[1]}


    # The shared users are read in the realm (admin API), never logged in.
    alice_groups, bob_groups = realm_groups("alice"), realm_groups("bob")
    check("alice (realm): jira read/write + proj-ABC", bool(alice_groups) and {"mcp-jira-read", "mcp-jira-write", "mcp-jira-proj-ABC"} <= alice_groups, str(alice_groups))
    check("bob (realm): jira read only", bool(bob_groups) and "mcp-jira-read" in bob_groups and "mcp-jira-write" not in bob_groups, str(bob_groups))
    check("carol (realm): mcp-platform-admin", "mcp-platform-admin" in (realm_groups("carol") or set()))
    check("mallory (realm): no mcp- groups", realm_groups("mallory") is not None and not any(g.startswith("mcp-") for g in realm_groups("mallory")))

    check("claude-code cannot ask for the broker audience scope",
          expect_fail(lambda: mcpdev.login(probe.name, scope="openid mcp-credential-broker-audience")))

    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{issuer}/clients-registrations/openid-connect", method="POST",
            data=json.dumps({"redirect_uris": ["http://localhost:1/cb"], "token_endpoint_auth_method": "none"}).encode(),
            headers={"Content-Type": "application/json"}))
        dcr_status = 201
    except urllib.error.HTTPError as e:
        dcr_status = e.code
    check("anonymous dynamic client registration rejected (explicit Trusted Hosts policy)", dcr_status == 403, str(dcr_status))

    print("== token exchange (C4)")
    gw_token = mcpdev.exchange(user_token, "mcp-gateway", "mcp-credential-broker")
    gw = mcpdev.decode_jwt(gw_token)
    check("gateway: user -> aud=mcp-credential-broker", gw.get("aud") == "mcp-credential-broker", str(gw.get("aud")))
    check("gateway: exchanged token keeps sub", gw.get("sub") == claims["sub"])
    bff = mcpdev.decode_jwt(mcpdev.exchange(mcpdev.login(probe.name, "mcp-portal-bff"), "mcp-portal-bff", "mcp-credential-broker"))
    check("bff: user -> aud=mcp-credential-broker, azp=mcp-portal-bff",
          bff.get("aud") == "mcp-credential-broker" and bff.get("azp") == "mcp-portal-bff")
    os_token = mcpdev.decode_jwt(mcpdev.exchange(gw_token, "mcp-credential-broker", "opensearch"))
    check("broker: subjectToken (aud=broker) -> aud=opensearch", os_token.get("aud") == "opensearch"
          and os_token.get("sub") == claims["sub"], str(os_token.get("aud")))
    check("gateway cannot exchange to opensearch", expect_fail(lambda: mcpdev.exchange(user_token, "mcp-gateway", "opensearch")))
    check("gateway cannot exchange to mcp-gateway-admin",
          expect_fail(lambda: mcpdev.exchange(user_token, "mcp-gateway", "mcp-gateway-admin")))
    check("gateway cannot turn a broker token into aud=opensearch",
          expect_fail(lambda: mcpdev.exchange(gw_token, "mcp-gateway", "opensearch")))
    check("bff cannot exchange a token issued to the gateway",
          expect_fail(lambda: mcpdev.exchange(gw_token, "mcp-portal-bff", "mcp-credential-broker")))
    check("claude-code (public) cannot exchange",
          expect_fail(lambda: mcpdev.exchange(user_token, "claude-code", "mcp-credential-broker")))
    check("bff cannot exchange the claude-code token",
          expect_fail(lambda: mcpdev.exchange(user_token, "mcp-portal-bff", "mcp-credential-broker")))
    try:
        no_aud = mcpdev.decode_jwt(mcpdev.exchange(user_token, "mcp-gateway"))
        check("gateway exchange without audience yields no component audience", not audiences(no_aud) & COMPONENT_AUDIENCES,
              str(no_aud.get("aud")))
    except mcpdev.Fail:
        check("gateway exchange without audience yields no component audience", True)

    # A component inside the network talks to http://keycloak:8080; `iss` must stay the same.
    form = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "client_id": "mcp-gateway",
        "client_secret": mcpdev.cfg("KC_CLIENT_MCP_GATEWAY_SECRET"), "subject_token": user_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": "mcp-credential-broker", "scope": "mcp-credential-broker-audience"})
    inside = compose_exec("jira-mock", "curl -s --data-binary @- http://keycloak:8080/realms/corp/protocol/openid-connect/token",
                          stdin=form)
    try:
        inside_claims = mcpdev.decode_jwt(json.loads(inside.stdout)["access_token"])
        check("exchange from inside the network: same iss", inside_claims.get("iss") == issuer, inside_claims.get("iss"))
    except (ValueError, KeyError) as e:
        check("exchange from inside the network: same iss", False, f"{e}: {inside.stdout[:200]}")

    user_token_for_mock = user_token
    if WRITE:
        check("wrong password of the probe user rejected", expect_fail(lambda: mcpdev.login(probe.name, password="wrong")))

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
status, _ = http_get(f"{JIRA}/rest/api/2/issue/ABC-1", {"Authorization": f"Bearer {user_token_for_mock}"})
check("data with an IdP token (passthrough) -> 401", status == 401, str(status))
status, _ = http_get(f"{JIRA}/rest/api/2/search?jql=project%3DABC", {"Authorization": "Bearer someone-elses-pat"})
check("data with an unknown PAT -> 401", status == 401, str(status))
rows = mcpdev.jira_auth()
check("journal shows which credential reached Jira (PAT vs passthrough)",
      any(r["credential"] == "PAT alice-pat" for r in rows) and any("passthrough" in r["credential"] for r in rows))

broker_ca = str(ROOT / "certs/ca/ca.crt")
bff_cert = ("--cert", str(ROOT / "certs/mcp-portal-bff/tls.crt"), "--key", str(ROOT / "certs/mcp-portal-bff/tls.key"))
other_ca_cert = ("--cert", str(ROOT / "certs/untrusted/mcp-gateway/tls.crt"), "--key", str(ROOT / "certs/untrusted/mcp-gateway/tls.key"))
if stub_mode:
    print("== broker stub over mTLS (C4)")
    port = mcpdev.cfg("BROKER_MOCK_PORT", "18443")
    broker = f"https://{BROKER_TLS_NAME}:{port}"
else:
    print("== broker over mTLS (C4, real component)")
    port = mcpdev.cfg("BROKER_PORT", "18100")
    broker = f"https://{BROKER_TLS_NAME}:{port}"
resolve = ("--resolve", f"{BROKER_TLS_NAME}:{port}:127.0.0.1")
check("broker without client certificate: handshake refused (a closed port is no proof)", tls_refused(curl_status("--cacert", broker_ca, *resolve, f"{broker}/v1/systems")))
check("broker with a certificate of another CA: handshake refused", tls_refused(curl_status("--cacert", broker_ca, *resolve, *other_ca_cert, f"{broker}/v1/systems")))
mismatch = curl_status("--cacert", broker_ca, "--resolve", f"wrong-name.internal:{port}:127.0.0.1", *bff_cert, f"https://wrong-name.internal:{port}/v1/systems")
check("the broker certificate is checked against the name (curl exit 60, on the listening port)", mismatch.returncode == 60, f"exit {mismatch.returncode}")
if not stub_mode:
    notoken = curl_status("--cacert", broker_ca, *resolve, *bff_cert, f"{broker}/v1/systems")
    check("broker with the BFF certificate but without a bearer token -> 401", notoken.stdout == "401", notoken.stdout)
print("== OpenBao (read only: policy, AppRole, Transit key, audit device)")
root_in = mcpdev.cfg("BAO_ROOT_TOKEN") + "\n"
as_root = "read -r BAO_TOKEN; export BAO_TOKEN; "
role = compose_exec("openbao", as_root + "bao read -field=token_policies auth/approle/role/mcp-credential-broker", stdin=root_in)
check("AppRole mcp-credential-broker exists (init has run)", role.returncode == 0 and role.stdout.strip() != "", role.stderr[-200:])
key = compose_exec("openbao", as_root + "bao read -field=type transit/keys/mcp-credential-broker", stdin=root_in)
check("Transit key mcp-credential-broker exists", key.returncode == 0 and key.stdout.strip() != "", key.stderr[-200:])
audit_list = compose_exec("openbao", as_root + "bao audit list", stdin=root_in)
check("audit device enabled", "stdout/" in audit_list.stdout, audit_list.stderr[-200:])

print("== Redis ACL per component (read only)")
noauth = compose_exec("redis", "redis-cli ping")
check("default user disabled (NOAUTH)", "NOAUTH" in noauth.stdout + noauth.stderr, noauth.stdout)
users = compose_exec("redis", "read -r REDISCLI_AUTH; export REDISCLI_AUTH; redis-cli --no-auth-warning --user admin ACL USERS",
                     stdin=mcpdev.cfg("REDIS_ADMIN_PASSWORD") + "\n").stdout.split()
check("ACL users of the components exist", {"gateway", "broker", "mcp-jira", "portal-bff"} <= set(users), str(users))

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

for method, path in (("GET", "/__admin/mappings"), ("POST", "/__admin/mappings"), ("POST", "/__admin/reset")):
    res = run("docker", "run", "--rm", "--network", f"{PROJECT}-servers", "--entrypoint", "curl", CURL_IMAGE,
              "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "5", "-X", method,
              "-H", "Content-Type: application/json", "-d", "{}" if method == "POST" else "",
              f"http://jira.internal:8080{path}")
    check(f"servers network: Jira mock {method} {path} without admin credentials -> 401", res.stdout == "401",
          res.stdout)

if WRITE and stub_mode:
    with mcpdev.TempUser(["mcp-client-claude-code"], otp=False) as writer:
        status, body = mcpdev.put_secret(writer.name, "jira", "alice-pat")
        check(f"put-secret to the stub -> 200 connected (TLS name {BROKER_TLS_NAME} verified)", status == 200 and body.get("status") == "connected", f"{status} {body}")

if WRITE:
    print("== writes (--write): Redis keys with a TTL, an OpenBao path, a Jira mock mapping")

    def redis_as(user, *commands):
        script = "read -r REDISCLI_AUTH; export REDISCLI_AUTH; " + "; ".join(
            f"redis-cli --no-auth-warning --user {user} {c}" for c in commands)
        pw = mcpdev.cfg(f"REDIS_{user.upper().replace('-', '_')}_PASSWORD")
        return [line for line in compose_exec("redis", script, stdin=pw + "\n").stdout.splitlines() if line.strip()]

    out = redis_as("gateway", "set gw:smoke 1 EX 60", "set broker:smoke 1 EX 60", "flushall")
    check("gateway: own prefix gw:*", out[0] == "OK", str(out))
    check("gateway: no broker keys, no flushall", all("NOPERM" in o for o in out[1:]), str(out))
    out = redis_as("broker", "set broker:smoke 1 EX 60", "set jti:mcp-credential-broker:smoke 1 EX 60", "set gw:smoke 1 EX 60",
                   "publish other.channel x")
    check("broker: broker:*, jti:mcp-credential-broker:*", out[:2] == ["OK", "OK"], str(out))
    check("broker: no gateway keys, no other channels", all("NOPERM" in o for o in out[2:]), str(out))
    out = redis_as("mcp-jira", "set jti:mcp-jira:smoke 1 EX 60", "set jti:mcp-confluence:smoke 1 EX 60", "publish other.channel x")
    check("mcp-jira: only jti:mcp-jira:*", out[0] == "OK" and all("NOPERM" in o for o in out[1:]), str(out))
    out = redis_as("mcp-jira", "scan 0", "keys '*'", "randomkey", "eval 'return 1' 0", "script flush", "get gw:smoke")
    check("mcp-jira: no SCAN, KEYS, RANDOMKEY, EVAL, SCRIPT, no gw:* keys", len(out) == 6 and all("NOPERM" in o for o in out), str(out))
    out = redis_as("gateway", "scan 0", "keys '*'")
    check("gateway: no SCAN, KEYS", len(out) == 2 and all("NOPERM" in o for o in out), str(out))

    login_out = compose_exec(
        "openbao", f"bao write -field=token auth/approle/login role_id={mcpdev.cfg('BAO_BROKER_ROLE_ID')} secret_id=-",
        stdin=mcpdev.cfg("BAO_BROKER_SECRET_ID"))
    broker_token = login_out.stdout.strip()
    check("approle mcp-credential-broker login", login_out.returncode == 0 and broker_token, login_out.stderr[-200:])
    as_broker = "read -r BAO_TOKEN; export BAO_TOKEN; "
    enc = compose_exec("openbao", as_broker + "bao write -field=ciphertext transit/encrypt/mcp-credential-broker plaintext=dGVzdA==",
                       stdin=broker_token + "\n")
    check("broker token can use transit key", enc.stdout.startswith("vault:v1:"), enc.stderr[-200:])
    # The path expires by itself (delete-version-after) and its metadata is removed at the end, so no version stays in the raft.
    smoke_path = "mcp/connections/smoke/jira"
    compose_exec("openbao", as_root + f"bao kv metadata put -mount=secret -delete-version-after=60s {smoke_path} >/dev/null", stdin=root_in)
    kv = compose_exec("openbao", as_broker + f"bao kv put -mount=secret {smoke_path} probe=1 >/dev/null", stdin=broker_token + "\n")
    check("broker token can write KV v2 connections path", kv.returncode == 0, kv.stderr[-200:])
    compose_exec("openbao", as_root + f"bao kv metadata delete -mount=secret {smoke_path} >/dev/null", stdin=root_in)
    deny = compose_exec("openbao", as_broker + "bao kv get -mount=secret other/path", stdin=broker_token + "\n")
    check("broker token denied outside its path", deny.returncode != 0 and "permission denied" in deny.stderr)

    admin = {"Authorization": "Basic " + base64.b64encode(
        f"admin:{mcpdev.cfg('JIRA_MOCK_ADMIN_PASSWORD')}".encode()).decode(), "Content-Type": "application/json"}
    probe_mapping = {"request": {"urlPath": "/smoke-proxy-probe"}, "response": {"proxyBaseUrl": "http://keycloak:8080"}}
    with urllib.request.urlopen(urllib.request.Request(f"{JIRA}/__admin/mappings", data=json.dumps(probe_mapping).encode(),
                                                       headers=admin, method="POST")) as resp:
        probe_id = json.load(resp)["id"]
    try:
        status, body = http_get(f"{JIRA}/smoke-proxy-probe")
    finally:
        urllib.request.urlopen(urllib.request.Request(f"{JIRA}/__admin/mappings/{probe_id}", headers=admin, method="DELETE"))
    check("Jira mock refuses to proxy to the core network", status != 200 and "keycloak" not in body.lower(), f"{status} {body[:120]}")

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


def audit_exit_files(chunks):
    paths = []
    for chunk in chunks:
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("\n".join(chunk))
        paths.append(f.name)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return mcpdev.audit(rid, paths)
    finally:
        for path in paths:
            Path(path).unlink()


check("audit: events merged from several --logs files", audit_exit_files([lines[:1], lines[1:2], lines[2:]]) == 0)

print()
if failures:
    print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
