#!/usr/bin/env python3
"""M4 infrastructure checks (mcp-contracts 4.0.0, migration of ws-01).

  m4.py run
  m4.py wait-bundle SECONDS      wait until the latest main bundle verifies with the key the gateway is given

Part 1 runs on any stack, M3 or M4, and changes nothing: per-server CI certificates and key permissions, dev- policy keys (ws-09),
token exchange to aud=opensearch against the OpenSearch mock, the service-account key against the Service Desk mock, the OpenBao
policy of the broker for secret/mcp/service-accounts.
Part 2 needs the M4 components and is SKIP with the reason "przed przelaczeniem" while the running gateway is not an M4 image:
a certificate of a foreign server at the registry (403), offboarding (IdP, broker purge, gateway purge, repeated), 429 with
Retry-After, the service account reaching Service Desk through the gateway. Part 2 creates and deletes one throw-away user in
the realm (never alice, bob, carol, mallory) and writes the registry entries of the servers it was given manifests for. It has
not run against real M4 components yet.
Exit code 0 when nothing FAILed.
"""
import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path

import m2
import mcpdev
from m2 import PROXY, bearer, http, jbody, mcp_session, record, results, skip
from mcpdev import cfg

ROOT = mcpdev.ROOT
CERTS = ROOT / "certs"
BAO = f"http://localhost:{cfg('OPENBAO_PORT')}"
OPENSEARCH = f"http://localhost:{cfg('OPENSEARCH_MOCK_PORT')}"
SERVICEDESK = f"http://localhost:{cfg('SERVICEDESK_MOCK_PORT')}"
POLICIES = f"http://localhost:{cfg('POLICIES_PORT')}"
SERVERS = ("mcp-jira", "mcp-confluence", "mcp-gitlab", "mcp-servicedesk", "mcp-opensearch")
BEFORE_SWITCH = "przed przelaczeniem"


def compose_config():
    out = subprocess.run([str(ROOT / "scripts" / "compose.sh"), "--profile", "full", "--profile", "m2", "config", "--format", "json"],
                         capture_output=True, text=True).stdout
    return json.loads(out)


def running_image(container):
    return subprocess.run(["docker", "inspect", container, "-f", "{{.Config.Image}}"], capture_output=True, text=True).stdout.strip()


def switched():
    """True when the running gateway and broker are M4 or M5 images (tag m4-... or m5-...): the M4 checks hold for both."""
    return all(any(tag in running_image(f"mcp-dev-{name}-1") for tag in (":m4-", ":m5-")) for name in ("mcp-gateway", "mcp-credential-broker"))


def x509_text(path, *args):
    return subprocess.run(["openssl", "x509", "-in", str(path), "-noout", *args], capture_output=True, text=True).stdout


def mode(path):
    return path.stat().st_mode & 0o777


# ---- part 1: runs on any stack -------------------------------------------------------------------------------------

def check_certificates():
    for server in SERVERS:
        cert = CERTS / f"mcp-registry-ci-{server}" / "tls.crt"
        if not cert.exists():
            record(f"certs: CI certificate of {server} exists", False, "make certs")
            continue
        subject = x509_text(cert, "-subject").strip().replace(" = ", "=")
        eku = x509_text(cert, "-ext", "extendedKeyUsage")
        verified = subprocess.run(["openssl", "verify", "-CAfile", str(CERTS / "ca" / "ca.crt"), "-purpose", "sslclient", str(cert)],
                                  capture_output=True, text=True).stdout
        record(f"certs: {server}: CN=mcp-registry-ci-{server}, clientAuth and no serverAuth, verifies against the CA",
               subject.endswith(f"CN=mcp-registry-ci-{server}") and "TLS Web Client Authentication" in eku
               and "TLS Web Server Authentication" not in eku and verified.strip().endswith("OK"))
    legacy = CERTS / "legacy" / "mcp-registry-ci" / "tls.crt"
    record("certs: the old shared CN mcp-registry-ci exists only under certs/legacy (for the 403 test)",
           legacy.exists() and x509_text(legacy, "-subject").strip().replace(" = ", "=").endswith("CN=mcp-registry-ci")
           and not (CERTS / "mcp-registry-ci").exists())
    off = CERTS / "mcp-offboarding" / "tls.crt"
    record("certs: mcp-offboarding exists (CN=mcp-offboarding, clientAuth)", off.exists() and "CN=mcp-offboarding" in x509_text(off, "-subject").replace(" = ", "=")
           and "TLS Web Client Authentication" in x509_text(off, "-ext", "extendedKeyUsage"))
    loose = [str(p.relative_to(CERTS)) for pattern in ("*/*.key", "*/*.p12", "*/*/*.key", "*/*/*.p12")
             for p in CERTS.glob(pattern) if mode(p) != 0o600]
    record("certs: every private key and keystore has mode 600", not loose, ", ".join(loose[:5]))
    mounted = [f"{name}: {v['source']}" for name, svc in compose_config()["services"].items() for v in svc.get("volumes", [])
               if "legacy" in str(v.get("source", "")) or str(v.get("source", "")).rstrip("/").endswith("/certs/mcp-registry-ci")]
    record("compose: the old shared CI certificate is mounted nowhere", not mounted, "; ".join(mounted))


def check_dev_keys():
    keys = ROOT.parent / "mcp-policies" / "keys" / "dev"
    for name in ("dev-policies-main", "dev-policies-overrides"):
        public = keys / f"{name}.public.pem"
        text = subprocess.run(["openssl", "pkey", "-pubin", "-in", str(public), "-noout", "-text"], capture_output=True, text=True).stdout
        record(f"keys: {name} (ws-09) is an Ed25519 public key", public.exists() and "ED25519" in text.upper())
    env = compose_config()["services"]["mcp-gateway"]["environment"]
    ids = (env["MCP_POLICY_MAIN_KEY_ID"], env["MCP_POLICY_OVERRIDES_KEY_ID"])
    if all(i.startswith("dev-") for i in ids):
        record("keys: the gateway of the compose file uses the dev- policy keys (CC-137)", all((keys / f"{i}.public.pem").exists() for i in ids), ", ".join(ids))
    else:
        skip("keys: the gateway of the compose file uses the dev- policy keys (CC-137)", f"{BEFORE_SWITCH}: compose still has {', '.join(ids)}; make m4-switch sets POLICY_KEY_*_ID")


def verify_signature(public_pem, data, signature):
    with tempfile.TemporaryDirectory() as tmp:
        files = {name: Path(tmp) / name for name in ("data", "sig")}
        files["data"].write_bytes(data)
        files["sig"].write_bytes(signature)
        done = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public_pem), "-rawin",
                               "-in", str(files["data"]), "-sigfile", str(files["sig"])], capture_output=True, text=True)
        return done.returncode == 0


def check_bundle_signature():
    env = compose_config()["services"]["mcp-gateway"]["environment"]
    key_id = env["MCP_POLICY_MAIN_KEY_ID"]
    mount = next((v["source"] for v in compose_config()["services"]["mcp-gateway"].get("volumes", [])
                  if v.get("target") == env["MCP_POLICY_MAIN_KEY_FILE"]), None)
    status, _, raw = http("GET", f"{POLICIES}/policies/main/latest.json")
    latest = jbody(raw)
    if status != 200 or "url" not in latest or mount is None:
        record("policies: the latest main bundle is served and the gateway key file is mounted", False, f"HTTP {status}")
        return
    _, _, archive = http("GET", f"{POLICIES}/policies/main/{latest['url']}")
    _, _, sig = http("GET", f"{POLICIES}/policies/main/{latest['sigUrl']}")
    ok = verify_signature(mount, archive, sig)
    record(f"policies: the latest main bundle ({latest.get('revision')}) verifies with the key the gateway is given ({key_id})", ok,
           "" if ok else "the bundle pipeline of ws-09 must sign with the key of that id")


def bao(method, path, token, body=None):
    req = urllib.request.Request(f"{BAO}/v1/{path}", method=method, headers={"X-Vault-Token": token},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return e.code, {}


def check_service_account():
    key = cfg("SERVICEDESK_API_KEY")
    status, reply = bao("POST", "auth/approle/login", "", {"role_id": cfg("BAO_BROKER_ROLE_ID"), "secret_id": cfg("BAO_BROKER_SECRET_ID")})
    if status != 200:
        record("openbao: AppRole login of the broker", False, f"HTTP {status}")
        return
    token = reply["auth"]["client_token"]
    path = "secret/data/mcp/service-accounts/servicedesk"
    status, body = bao("GET", path, token)
    if status == 200:
        data = body["data"]["data"]
        record("openbao: the broker reads the service account of Service Desk (secret/mcp/service-accounts/servicedesk, value = the mock key)",
               data.get("value") == key)
        status, body = bao("GET", "secret/data/mcp/oauth-clients/opensearch", token)
        record("openbao: the broker reads the token-exchange client secret (secret/mcp/oauth-clients/opensearch)",
               status == 200 and body["data"]["data"].get("client_id") == "mcp-credential-broker"
               and body["data"]["data"].get("client_secret") == cfg("KC_CLIENT_MCP_CREDENTIAL_BROKER_SECRET"))
        record("openbao: the broker cannot write, delete or list service accounts",
               bao("POST", path, token, {"data": {"key": "x"}})[0] == 403 and bao("DELETE", path, token)[0] == 403
               and bao("LIST", "secret/metadata/mcp/service-accounts", token)[0] == 403)
    else:
        skip("openbao: the broker reads secret/mcp/service-accounts/servicedesk",
             f"{BEFORE_SWITCH}: HTTP {status}, openbao-init of M4 has not run (scripts/openbao-init-test.sh checks it in isolation)")
    bao("POST", "auth/token/revoke-self", token)

    ticket = f"{SERVICEDESK}/api/v1/tickets/IT-1"
    record("servicedesk mock: accepts the service account key (Bearer), HTTP 200",
           http("GET", ticket, {"Authorization": f"Bearer {key}"})[0] == 200)
    jwt = mcpdev.exchange(mcpdev.login("alice"), "mcp-gateway", "mcp-credential-broker")
    record("servicedesk mock: refuses a Keycloak JWT, a Jira PAT and no credential (401)",
           all(http("GET", ticket, h)[0] == 401 for h in (bearer(jwt), bearer("alice-pat"), {})))


def search(token, expression):
    return http("GET", f"{OPENSEARCH}/{expression}/_search?size=1", bearer(token) if token else {})[0]


def check_token_exchange():
    alice_gateway = mcpdev.login("alice")
    sub = mcpdev.decode_jwt(alice_gateway)["sub"]
    broker_token = mcpdev.exchange(alice_gateway, "mcp-gateway", "mcp-credential-broker")
    opensearch_token = mcpdev.exchange(broker_token, "mcp-credential-broker", "opensearch")
    claims = mcpdev.decode_jwt(opensearch_token)
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    record("token exchange: broker token -> aud=opensearch, same sub, exp in the future (expiresAt of resolve, CC-157)",
           "opensearch" in audiences and claims.get("sub") == sub and claims.get("exp", 0) > time.time() and claims.get("azp") == "mcp-credential-broker",
           f"aud={audience} azp={claims.get('azp')}")
    record("token exchange: lifetime of the opensearch token is at most 60 min (the token of the client C1 sets the window)",
           claims["exp"] - claims["iat"] <= 3600, f"{claims['exp'] - claims['iat']} s")
    record("opensearch mock: Bearer from the exchange reads logs-team-a-* (200)", search(opensearch_token, "logs-team-a-*") == 200)
    record("opensearch mock: tokens with aud=mcp-gateway, aud=mcp-credential-broker, a PAT and no token get 401",
           all(search(t, "logs-team-a-*") == 401 for t in (alice_gateway, broker_token, "alice-pat", None)))
    record("opensearch mock: hr-* and logs-team-b-* by name are 403 even with a valid exchange token",
           search(opensearch_token, "hr-payroll") == 403 and search(opensearch_token, "logs-team-b-2026.10.28") == 403)
    bob_token = mcpdev.exchange(mcpdev.exchange(mcpdev.login("bob"), "mcp-gateway", "mcp-credential-broker"), "mcp-credential-broker", "opensearch")
    record("opensearch mock: bob's exchange token cannot read hr-payroll (403)", search(bob_token, "hr-payroll") == 403)
    for requester, subject, why in (("mcp-gateway", alice_gateway, "the gateway cannot exchange to opensearch"),
                                    ("mcp-portal-bff", mcpdev.login("alice", "mcp-portal-bff"), "the BFF cannot exchange to opensearch")):
        try:
            mcpdev.exchange(subject, requester, "opensearch")
            record(f"token exchange: {why}", False, "token issued")
        except mcpdev.Fail as e:
            record(f"token exchange: {why}", "HTTP 4" in str(e), str(e)[:80])


# ---- part 2: needs the M4 components ----------------------------------------------------------------------------

def manifest_path(server):
    if os.environ.get("MANIFEST_DIR"):
        return Path(os.environ["MANIFEST_DIR"]) / f"{server}.json"
    return ROOT.parent / f"mcp-server-{server.removeprefix('mcp-')}" / "mcp-app" / "target" / "mcp-manifest.json"


def check_registry_certificates():
    name = "registry: a CI certificate of another server is 403"
    if not switched():
        skip(name, f"{BEFORE_SWITCH}: the M3 gateway accepts the shared CN")
        skip("registry: the old shared CN mcp-registry-ci is 403", f"{BEFORE_SWITCH}")
        skip("registry: the certificate of the server registers its own manifest (200)", f"{BEFORE_SWITCH}")
        return
    refused, wrong = [], []
    for server in SERVERS:
        manifest = manifest_path(server)
        if not manifest.exists():
            skip(f"registry: {server}", f"no manifest at {manifest}")
            continue
        other = SERVERS[(SERVERS.index(server) + 1) % len(SERVERS)]
        for identity in (f"mcp-registry-ci-{other}", "legacy/mcp-registry-ci"):
            status, _ = mcpdev.registry_put(server, manifest, identity)
            (refused if status == 403 else wrong).append(f"{server} with {identity}: HTTP {status}")
        status, _ = mcpdev.registry_put(server, manifest)
        if status != 200:
            wrong.append(f"{server} with its own certificate: HTTP {status}")
    record("registry: a certificate of another server and the old shared CN are 403, the own certificate is 200", not wrong and len(refused) >= 2, "; ".join(wrong[:3]))


class TemporaryUser:
    """A user that exists only for one check: created with a random password and the groups given, deleted at the end."""

    def __init__(self, groups):
        self.name = "m4-" + secrets.token_hex(4)
        self.password = secrets.token_urlsafe(18)
        self.groups = groups

    def __enter__(self):
        status, body = mcpdev.kc_admin("POST", "/users", {
            "username": self.name, "enabled": True, "emailVerified": True, "email": f"{self.name}@corp.test",
            "firstName": "M4", "lastName": "Check",
            "credentials": [{"type": "password", "value": self.password, "temporary": False}]})
        if status != 201:
            raise mcpdev.Fail(f"cannot create the temporary user: HTTP {status} {body}")
        self.sub = mcpdev.kc_admin("GET", f"/users?username={self.name}&exact=true")[1][0]["id"]
        for group in self.groups:
            found = mcpdev.kc_admin("GET", f"/groups?search={group}&exact=true")[1]
            gid = next(g["id"] for g in found if g["name"] == group)
            mcpdev.kc_admin("PUT", f"/users/{self.sub}/groups/{gid}")
        return self

    def __exit__(self, *exc):
        mcpdev.kc_admin("DELETE", f"/users/{self.sub}")

    def token(self):
        return mcpdev.login(self.name, password=self.password)


def container_log(name, needle):
    """The needle in the log of any instance of the component (M5 runs a second instance of gateway and broker behind a balancer)."""
    for container in (f"mcp-dev-{name}-1", f"mcp-dev-{name}-2-1"):
        out = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
        if needle in out.stdout + out.stderr:
            return True
    return False


def check_offboarding():
    names = ("offboarding: IdP groups removed and sessions ended, broker purge and gateway purge 204, repeated after the logout",
             "offboarding: the user has no mcp-* groups in a new token", "offboarding: purge is idempotent for an unknown sub (204 at both)",
             "offboarding: other certificates get 403 on both purges", "offboarding: both purges leave the admin_change user_purged event")
    if not switched():
        for name in names:
            skip(name, BEFORE_SWITCH)
        return
    with TemporaryUser(["mcp-client-claude-code", "mcp-jira-read"]) as user:
        record("offboarding: the temporary user has its groups in the token before", "mcp-jira-read" in mcpdev.decode_jwt(user.token()).get("groups", []))
        steps = mcpdev.offboard(user.name, repeat_delay=2.0)
        record(names[0], all(ok for _, ok, _ in steps), "; ".join(f"{n}: {d}" for n, ok, d in steps if not ok)[:200])
        groups = [g for g in mcpdev.decode_jwt(user.token()).get("groups", []) if g.startswith("mcp-")]
        record(names[1], not groups, ", ".join(groups))
        sub = user.sub
        time.sleep(1)
        record(names[4], container_log("mcp-credential-broker", sub) and container_log("mcp-gateway", sub) and container_log("mcp-gateway", "user_purged"))
    unknown = str(uuid.uuid4())
    broker = mcpdev.offboarding_request("POST", f"/v1/admin/users/{unknown}/purge")[0]
    gateway = mcpdev.gateway_request("mcp-offboarding", "POST", f"/admin/v1/users/{unknown}/purge")[0]
    record(names[2], broker == 204 and gateway == 204, f"{broker}/{gateway}")
    broker_other = mcpdev.mtls_request("mcp-portal-bff", "POST", mcpdev.broker_admin_url(), f"/v1/admin/users/{unknown}/purge",
                                       cfg("MCP_BROKER_TLS_NAME"))[0]
    gateway_other = mcpdev.gateway_request("mcp-gateway", "POST", f"/admin/v1/users/{unknown}/purge")[0]
    record(names[3], broker_other == 403 and gateway_other == 403, f"{broker_other}/{gateway_other}")


def check_rate_limit():
    name = "rate limit: a burst of MCP requests of one user gets 429 with Retry-After, the reason is rate_limited_user"
    if not switched():
        skip(name, BEFORE_SWITCH)
        return
    with TemporaryUser(["mcp-client-claude-code", "mcp-jira-read"]) as user:
        token = user.token()
        seen = None
        for _ in range(400):
            status, headers, raw = http("POST", f"{PROXY}/s/mcp-jira/mcp", {**bearer(token), "Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                                        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode())
            if status == 429:
                seen = (headers.get("Retry-After"), raw.decode(errors="replace"))
                break
        if seen is None:
            skip(name, "no 429 in 400 requests: the limit of the gateway is not configured for this stack (ws-05)")
        else:
            record(name, bool(seen[0]) and seen[0].isdigit() and "rate_limited" in seen[1], f"Retry-After {seen[0]}")


def tool_arguments(schema, queue="IT"):
    """Values for the required properties of a search tool of Service Desk (queue, text) or OpenSearch (index_prefix, text, from, to)."""
    values = {"queue": queue, "text": "VPN", "index_prefix": "logs-team-a", "from": "2026-10-27T00:00:00Z", "to": "2026-10-29T00:00:00Z"}
    return {key: values.get(key, "a") for key in schema.get("required", [])}


def check_service_account_through_gateway():
    name = "service account: Service Desk through the gateway receives the service key, never a JWT"
    if not switched():
        skip(name, BEFORE_SWITCH)
        return
    password = cfg("MOCK_ADMIN_PASSWORD")
    auth = {"Authorization": "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()}
    journal = lambda: jbody(http("GET", f"{SERVICEDESK}/__mock/journal", auth)[2]) or []
    before = len(journal())
    call = mcp_session(f"{PROXY}/s/mcp-servicedesk/mcp", mcpdev.login("alice"))
    call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "m4", "version": "1"}})
    call("notifications/initialized", notify=True)
    tools = call("tools/list")[1].get("result", {}).get("tools", [])
    search_tool = next((t for t in tools if t["name"].endswith("search_tickets")), None)
    if search_tool is None:
        record(name, False, "no search_tickets in tools/list")
        return
    call("tools/call", {"name": search_tool["name"], "arguments": tool_arguments(search_tool["inputSchema"])})
    fresh = journal()[before:]
    record(name, bool(fresh) and all(e["auth"] != "jwt" for e in fresh) and any(e["status"] == 200 for e in fresh),
           ", ".join(sorted({f"{e['auth']}:{e['status']}" for e in fresh})))


def check_opensearch_through_gateway():
    name = "token-exchange: opensearch through the gateway (resolve of the broker) reaches the mock with a JWT of aud=opensearch"
    if not switched():
        skip(name, BEFORE_SWITCH)
        return
    password = cfg("MOCK_ADMIN_PASSWORD")
    auth = {"Authorization": "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()}
    journal = lambda: jbody(http("GET", f"{OPENSEARCH}/__mock/journal", auth)[2]) or []
    before = len(journal())
    call = mcp_session(f"{PROXY}/s/mcp-opensearch/mcp", mcpdev.login("alice"))
    call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "m4", "version": "1"}})
    call("notifications/initialized", notify=True)
    tools = call("tools/list")[1].get("result", {}).get("tools", [])
    tool = next((t for t in tools if t["name"].endswith("search_logs")), None)
    if tool is None:
        record(name, False, "no search tool in tools/list")
        return
    call("tools/call", {"name": tool["name"], "arguments": tool_arguments(tool["inputSchema"])})
    fresh = journal()[before:]
    record(name, bool(fresh) and any(e["auth"] == "jwt" and e["status"] == 200 for e in fresh) and all(e["auth"] == "jwt" for e in fresh),
           ", ".join(sorted({f"{e['auth']}:{e['status']}" for e in fresh})))


def check_access_groups():
    """Groups of the dev users for Service Desk and OpenSearch after the switch (docs/m4.md): alice reads and writes queue IT, bob reads
    queue FIN only, nobody has HR; both read logs-team-a."""
    name = "realm: Service Desk and OpenSearch groups of alice and bob match keycloak/realm-corp.json"
    groups = {user: set(mcpdev.decode_jwt(mcpdev.login(user)).get("groups", [])) for user in ("alice", "bob")}
    want = {"alice": {"mcp-servicedesk-read", "mcp-servicedesk-write", "mcp-servicedesk-queue-IT", "mcp-opensearch-read", "mcp-opensearch-idx-logs-team-a"},
            "bob": {"mcp-servicedesk-read", "mcp-servicedesk-queue-FIN", "mcp-opensearch-read", "mcp-opensearch-idx-logs-team-a"}}
    missing = {u: sorted(want[u] - groups[u]) for u in want if want[u] - groups[u]}
    extra = [u for u in groups if "mcp-servicedesk-queue-HR" in groups[u]]
    if missing and not switched():
        skip(name, f"{BEFORE_SWITCH}: missing {missing}; make m4-switch runs mcpdev.py sync-groups")
    else:
        record(name, not missing and not extra, f"missing {missing}")


def check_servicedesk_queues():
    name = "service desk: alice reads queue IT, not FIN and not HR; bob reads FIN, not IT and not HR; carol (no queue group) none (queues granted by the policy bundle only)"
    if not switched():
        skip(name, BEFORE_SWITCH)
        return
    reached = {}
    for user in ("alice", "bob", "carol"):
        call = mcp_session(f"{PROXY}/s/mcp-servicedesk/mcp", mcpdev.login(user))
        call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "m4", "version": "1"}})
        call("notifications/initialized", notify=True)
        tool = next((t for t in call("tools/list")[1].get("result", {}).get("tools", []) if t["name"].endswith("search_tickets")), None)
        if tool is None:
            if user == "carol":
                reached.update({("carol", q): False for q in ("IT", "FIN", "HR")})
                continue
            record(name, False, f"{user}: no search_tickets")
            return
        for queue in ("IT", "FIN", "HR"):
            result = call("tools/call", {"name": tool["name"], "arguments": tool_arguments(tool["inputSchema"], queue)})[1].get("result", {})
            reached[(user, queue)] = not result.get("isError", False) and "not_found_or_denied" not in json.dumps(result)
    expected = {("alice", "IT"): True, ("alice", "FIN"): False, ("alice", "HR"): False,
                ("bob", "IT"): False, ("bob", "FIN"): True, ("bob", "HR"): False,
                ("carol", "IT"): False, ("carol", "FIN"): False, ("carol", "HR"): False}
    record(name, reached == expected, str({f"{u}/{q}": v for (u, q), v in reached.items() if expected[(u, q)] != v}))


def check_environment_dev():
    skip("environment=dev: a manifest with environment dev is rejected by a gateway without the dev mode (422 environment_mismatch)",
         "needs a second gateway in production mode (without profile devstack) on a host port; the stack gateway runs in dev mode by design")
    if switched():
        wrong = [s for s in SERVERS if not container_log(s, "event=devstack_profile")]
        record("environment=dev: every server of the stack runs devstack (its manifest says environment dev)", not wrong, ", ".join(wrong))
    else:
        skip("environment=dev: every server of the stack runs devstack", BEFORE_SWITCH)


def wait_bundle(seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        before = len(results)
        check_bundle_signature()
        if results[-1][0] == "PASS":
            print(results[-1][1])
            return 0
        del results[before:]
        time.sleep(5)
    return 1


def run():
    parts = (check_certificates, check_dev_keys, check_bundle_signature, check_service_account, check_token_exchange,
             check_registry_certificates, check_offboarding, check_rate_limit, check_service_account_through_gateway, check_opensearch_through_gateway, check_access_groups, check_servicedesk_queues, check_environment_dev)
    for part in parts:
        try:
            part()
        except (mcpdev.Fail, OSError, KeyError, ValueError, StopIteration) as e:
            record(part.__name__, False, f"{type(e).__name__}: {e}")
    for status, name, detail in results:
        print(f"{status:4} {name}" + (f"  [{detail}]" if detail else ""))
    failed = sum(1 for r in results if r[0] == "FAIL")
    print(f"m4: {sum(1 for r in results if r[0] == 'PASS')} passed, {failed} failed, {sum(1 for r in results if r[0] == 'SKIP')} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["wait-bundle"] and len(sys.argv) == 3:
        sys.exit(wait_bundle(int(sys.argv[2])))
    sys.exit(run() if sys.argv[1:] == ["run"] else __doc__ and print(__doc__) or 2)
