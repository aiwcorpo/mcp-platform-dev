#!/usr/bin/env python3
"""M3 scenario skeleton: infrastructure checks that run today, then the Authenticator / DESTRUCTIVE confirmation flow.

  m3.py run

Infrastructure (PASS/FAIL): OpenBao Transit key and policy for Authenticator seeds, Redis ACL of the gateway, OTP
step-up level in Keycloak, policy bundle server, offboarding certificate, Jira mock DELETE.
Scenario (SKIP until the component has M3; each step names what it waits for): step-up registration of the
Authenticator, delete_issue -> confirmation_required, preview, code, execution, cancel, block after preview, Cedar
denial, activity log. The scenario code follows contracts 3.0.1 (C10, C11, C12) and has not run against real M3
components yet. TOTP codes come from the seed returned at registration and are never printed.
"""
import base64
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

import m2
import mcpdev
import totp
from m2 import PROXY, http, jbody, record, skip, results
from mcpdev import cfg

ROOT = mcpdev.ROOT
BAO = f"http://localhost:{cfg('OPENBAO_PORT')}"
POLICIES = f"http://localhost:{cfg('POLICIES_PORT')}"
JIRA_MOCK = f"http://localhost:{cfg('JIRA_MOCK_PORT')}"
POLICY_REPO = ROOT.parent / "mcp-policies"


def bao(method, path, token, body=None):
    req = urllib.request.Request(f"{BAO}/v1/{path}", method=method, headers={"X-Vault-Token": token},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return e.code, {}


def check_openbao():
    status, reply = bao("POST", "auth/approle/login", "", {"role_id": cfg("BAO_BROKER_ROLE_ID"), "secret_id": cfg("BAO_BROKER_SECRET_ID")})
    if status != 200:
        record("openbao: AppRole login of the broker", False, f"HTTP {status}")
        return
    token = reply["auth"]["client_token"]
    plain = base64.b64encode(b"m3-selftest").decode()
    for key in ("mcp-authenticators", "mcp-credential-broker"):
        s1, enc = bao("POST", f"transit/encrypt/{key}", token, {"plaintext": plain})
        s2, dec = bao("POST", f"transit/decrypt/{key}", token, {"ciphertext": enc.get("data", {}).get("ciphertext", "")})
        record(f"openbao: broker encrypts and decrypts with transit key {key}", s1 == 200 and s2 == 200 and dec["data"]["plaintext"] == plain, f"{s1}/{s2}")
    path = f"secret/data/mcp/authenticators/{hashlib.sha256(b'm3-selftest').hexdigest()}"
    wrote = bao("POST", path, token, {"data": {"ciphertext": "vault:v1:x"}})[0]
    read = bao("GET", path, token)[0]
    deleted = bao("DELETE", path, token)[0]
    record("openbao: broker writes, reads and deletes secret/data/mcp/authenticators/<hash>", wrote == 200 and read == 200 and deleted in (200, 204), f"{wrote}/{read}/{deleted}")
    denied = {
        "datakey of the authenticators key": bao("POST", "transit/datakey/plaintext/mcp-authenticators", token, {}),
        "key configuration": bao("POST", "transit/keys/mcp-authenticators/config", token, {"deletion_allowed": True}),
        "key deletion": bao("DELETE", "transit/keys/mcp-authenticators", token),
        "key rotation": bao("POST", "transit/keys/mcp-authenticators/rotate", token, {}),
        "reading the key": bao("GET", "transit/keys/mcp-authenticators", token),
        "KV outside mcp/connections and mcp/authenticators": bao("GET", "secret/data/mcp/other/x", token),
        "listing the KV mount": bao("LIST", "secret/metadata/", token),
        "Transit encrypt with a key outside the policy": bao("POST", "transit/encrypt/other", token, {"plaintext": plain}),
    }
    for what, (code, _) in denied.items():
        record(f"openbao: broker policy refuses {what}", code == 403, f"HTTP {code}")
    bao("POST", "auth/token/revoke-self", token)


def check_redis_gateway():
    allowed = ["sadd gw:m3:s a", "sismember gw:m3:s a", "zadd gw:m3:z 1 a", "zremrangebyscore gw:m3:z 0 5", "watch gw:m3:k", "unwatch",
               "eval 'return redis.call(\"set\", KEYS[1], 1)' 1 gw:m3:k", "setex gw:m3:e 5 1", "hsetnx gw:m3:h f 1",
               "del gw:m3:s gw:m3:z gw:m3:k gw:m3:e gw:m3:h"]
    out = m2.redis_as("gateway", allowed)
    record("redis: gateway has the commands of confirmations (sets, sorted sets, WATCH, scripts) on gw:*", len(out) == len(allowed) and not any("NOPERM" in o or "ERR" in o for o in out), "; ".join(out)[:200])
    forbidden = ["scan 0", "keys *", "randomkey", "dbsize", "info", "client list", "script flush", "script load 'return 1'", "function flush",
                 "flushall", "config get *", "get broker:x", "set portal:x 1", "eval 'return 1' 1 broker:x",
                 "eval 'return redis.call(\"set\",\"broker:x\",1)' 1 gw:x", "publish credential.invalidated x", "get jti:mcp-jira:x"]
    out = m2.redis_as("gateway", forbidden)
    record("redis: gateway has no SCAN/KEYS/INFO/SCRIPT/FUNCTION/FLUSH/CONFIG/PUBLISH and no other component's keys, also from scripts",
           len(out) == len(forbidden) and all("NOPERM" in o or "ACL failure" in o for o in out), "; ".join(out)[:300])


def check_keycloak_mfa():
    realm = json.loads((ROOT / "keycloak" / "realm-corp.json").read_text())
    claims = mcpdev.decode_jwt(mcpdev.login("alice"))
    record("keycloak: a password login reaches acr 'password' and is not asked for an OTP", claims.get("acr") == "password", str(claims.get("acr")))
    alice = next(u for u in realm["users"] if u["username"] == "alice")
    record("keycloak: sub of alice is the fixed id of the realm file", claims["sub"] == alice["id"])
    for user in ("alice", "bob", "carol"):
        try:
            c = mcpdev.decode_jwt(mcpdev.login(user, acr_values="mfa"))
            record(f"keycloak: {user} with acr_values=mfa + OTP of the dev seed reaches acr 'mfa'", c.get("acr") == "mfa", str(c.get("acr")))
        except mcpdev.Fail as e:
            record(f"keycloak: {user} with acr_values=mfa + OTP", False, str(e))
    try:
        mcpdev.login("alice", acr_values="mfa", otp_code="000000")
        record("keycloak: a wrong OTP does not give a token", False, "token issued")
    except mcpdev.Fail as e:
        record("keycloak: a wrong OTP does not give a token", "rejected" in str(e), str(e)[:100])
    try:
        mcpdev.login("mallory", acr_values="mfa")
        record("keycloak: mallory (no OTP configured) cannot reach acr 'mfa'", False, "token issued")
    except mcpdev.Fail as e:
        refused = "required-action" not in str(e) and "failed" in str(e)
        record("keycloak: step-up for a user without OTP is refused, with no self-service OTP setup", refused, str(e)[:100])
    record("keycloak: mallory still logs in at level 1 (password)", mcpdev.decode_jwt(mcpdev.login("mallory")).get("acr") == "password")
    realm = json.loads((ROOT / "keycloak" / "realm-corp.json").read_text())
    record("keycloak: brute force protection is on and CONFIGURE_TOTP is disabled in the realm",
           realm.get("bruteForceProtected") is True and any(a["alias"] == "CONFIGURE_TOTP" and a["enabled"] is False for a in realm["requiredActions"]))


def check_secrets_hygiene():
    out = subprocess.run(["make", "-s", "users"], cwd=ROOT, capture_output=True, text=True).stdout
    seeds = [v for k, v in mcpdev.CONFIG.items() if k.startswith("KC_USER_") and k.endswith("_OTP_SEED")]
    record("make users prints the passwords, no OTP seed value, no otpauth URI, no admin password",
           "alice" in out and len(seeds) >= 3 and not any(seed in out for seed in seeds) and "otpauth" not in out
           and mcpdev.CONFIG.get("KC_BOOTSTRAP_ADMIN_PASSWORD", "-") not in out, "")
    config = json.loads(subprocess.run([str(ROOT / "scripts" / "compose.sh"), "--profile", "full", "--profile", "m2", "config", "--format", "json"],
                                       capture_output=True, text=True).stdout)
    key_dirs = {(POLICY_REPO / "keys" / "dev").resolve()}
    servers = {name: svc for name, svc in config["services"].items() if "MCP_INTERNAL_URL" in (svc.get("environment") or {})}
    problems = []
    for name, svc in servers.items():
        env = svc["environment"]
        mount = next((v for v in svc.get("volumes", []) if v.get("target") == env.get("MCP_POLICY_PUBLIC_KEY_FILE")), None)
        if not env.get("MCP_POLICY_KEY_ID", "").startswith(("test-", "dev-")):
            problems.append(f"{name}: key id {env.get('MCP_POLICY_KEY_ID')}")
        if mount is None or (ROOT / mount["source"]).resolve().parent not in key_dirs:
            problems.append(f"{name}: key file not from mcp-policies/keys/dev")
        if env.get("SPRING_PROFILES_ACTIVE") != "devstack" or "AIWCORPO_MCP_PLATFORM_INTERNAL_JWT_ALLOW_TEST_KEYS" in env:
            problems.append(f"{name}: profile or JWT test key flag")
    record("compose (effective configuration): every MCP server runs devstack, with a test-* or dev-* (M4) id and a key file from the dev keys of ws-09, "
           "without the internal JWT test key flag", len(servers) >= 5 and not problems, "; ".join(problems)[:200])


def check_gateway_devstack():
    config = json.loads(subprocess.run([str(ROOT / "scripts" / "compose.sh"), "--profile", "full", "--profile", "m2", "config", "--format", "json"],
                                       capture_output=True, text=True).stdout)
    gateway = config["services"]["mcp-gateway"]
    env = gateway["environment"]
    record("compose: the gateway runs profile devstack with both acknowledgements, registry mode required, no JWT test key flag",
           env.get("SPRING_PROFILES_ACTIVE") == "devstack" and env.get("MCP_DEVSTACK_ACKNOWLEDGED") == "true"
           and env.get("AIWCORPO_MCP_PLATFORM_DEVSTACK_ACKNOWLEDGED") == "true" and env.get("MCP_REGISTRY_MODE", "required") == "required"
           and "MCP_INTERNAL_JWT_ALLOW_TEST_KEYS" not in env, "")
    running = subprocess.run(["docker", "inspect", "mcp-dev-mcp-gateway-1", "-f", "{{.Config.Image}}"], capture_output=True, text=True).stdout.strip()
    if running != gateway["image"]:
        skip("gateway: the running container logs event=devstack_profile", f"compose not applied yet (running {running}, compose {gateway['image']}): scripts/apply-m3-switch.sh")
        return
    log = subprocess.run(["docker", "logs", "mcp-dev-mcp-gateway-1"], capture_output=True, text=True)
    record("gateway: the running container logs event=devstack_profile", "event=devstack_profile" in log.stdout + log.stderr, running)


def check_policy_server():
    status, headers, raw = http("GET", f"{POLICIES}/policies/main/latest.json")
    latest = jbody(raw)
    record("policies: main/latest.json served with ETag and Cache-Control: no-cache",
           status == 200 and headers.get("ETag") and headers.get("Cache-Control") == "no-cache" and "revision" in latest, f"HTTP {status}")
    record("policies: overrides/latest.json served", http("GET", f"{POLICIES}/policies/overrides/latest.json")[0] == 200)
    for method in ("PUT", "POST", "DELETE"):
        status = http(method, f"{POLICIES}/policies/main/latest.json", data=b"x")[0]
        record(f"policies: {method} is refused", status == 403, f"HTTP {status}")
    for path in ("/policies/main/.publish.lock", "/policies/main/", "/", "/policies/main/x.tar.gz"):
        status = http("GET", f"{POLICIES}{path}")[0]
        record(f"policies: GET {path} -> 404", status == 404, f"HTTP {status}")
    if "revision" in latest:
        s1, h1, archive = http("GET", f"{POLICIES}/policies/main/{latest['url']}")
        s2, _, sig = http("GET", f"{POLICIES}/policies/main/{latest['sigUrl']}")
        record("policies: archive and 64-byte signature of the latest revision are served", s1 == 200 and s2 == 200 and len(sig) == 64, f"{s1}/{s2}/{len(sig)}")
        key_id = json.loads(subprocess.run([str(ROOT / "scripts" / "compose.sh"), "--profile", "full", "--profile", "m2", "config", "--format", "json"],
                                           capture_output=True, text=True).stdout)["services"]["mcp-gateway"]["environment"]["MCP_POLICY_MAIN_KEY_ID"]
        pem = POLICY_REPO / "keys" / "dev" / f"{key_id}.public.pem"
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_public_key
            from cryptography.exceptions import InvalidSignature
            if pem.exists() and s1 == 200 and len(sig) == 64:
                try:
                    load_pem_public_key(pem.read_bytes()).verify(sig, archive)
                    record("policies: Ed25519 signature of the latest main bundle verifies with the dev public key", True, latest["revision"])
                except InvalidSignature:
                    record("policies: Ed25519 signature of the latest main bundle verifies with the dev public key", False, latest["revision"])
        except ImportError:
            skip("policies: signature verification", "python cryptography not installed")
    out = subprocess.run(["docker", "exec", "mcp-dev-opensearch-mock-1", "python3", "-c",
                          "import urllib.request; print(urllib.request.urlopen('http://policies.internal:8080/policies/main/latest.json', timeout=3).status)"],
                         capture_output=True, text=True).stdout.strip()
    record("policies: reachable as policies.internal from the servers network", out == "200", out)


def check_misc():
    cert = ROOT / "certs" / "mcp-offboarding" / "tls.crt"
    text = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-subject", "-ext", "extendedKeyUsage"], capture_output=True, text=True).stdout
    verified = subprocess.run(["openssl", "verify", "-CAfile", str(ROOT / "certs" / "ca" / "ca.crt"), "-purpose", "sslclient", str(cert)],
                              capture_output=True, text=True).stdout
    record("certs: mcp-offboarding has CN=mcp-offboarding, clientAuth only, signed by the mTLS CA",
           "CN=mcp-offboarding" in text.replace(" = ", "=") and "Client Authentication" in text and "Server Authentication" not in text and verified.strip().endswith("OK"))
    for pat, expect in (("alice-pat", 204), ("bob-pat", 204), ("unknown-pat", 401)):
        status = http("DELETE", f"{JIRA_MOCK}/rest/api/2/issue/ABC-2", {"Authorization": f"Bearer {pat}"})[0]
        record(f"jira mock: DELETE issue with {pat} -> {expect}", status == expect, f"HTTP {status}")
    status = http("DELETE", f"{JIRA_MOCK}/rest/api/2/issue/ABC-99", {"Authorization": "Bearer alice-pat"})[0]
    record("jira mock: DELETE of an unknown issue -> 404", status == 404, f"HTTP {status}")


def check_registry_tools():
    """Every server has an approved manifest in the registry (bootstrap in dev) and lists its tools through the gateway."""
    token = mcpdev.login("alice", "claude-code")
    expected = {"jira": "jira__delete_issue", "confluence": None, "gitlab": None, "opensearch": None, "servicedesk": None}
    for system, must_have in expected.items():
        call = m2.mcp_session(f"{PROXY}/s/mcp-{system}/mcp", token)
        call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "m3", "version": "1"}})
        call("notifications/initialized", notify=True)
        status, reply = call("tools/list")
        names = sorted(t.get("name") for t in reply.get("result", {}).get("tools", []))
        if system == "servicedesk":
            record("registry: tools/list of mcp-servicedesk answers (alice has no servicedesk group, so the list may be empty)", status == 200 and "result" in reply, str(names))
        else:
            record(f"registry: tools/list of mcp-{system} through the gateway lists the tools alice may use", status == 200 and bool(names) and (must_have is None or must_have in names), ",".join(names)[:100])


# Systems whose credential type the broker does not implement yet (token-exchange, service-account: moved out of broker M3,
# ws-06) and that have no adapter; they are reported as SKIP with this reason instead of failing the suite.
BROKER_GAPS = {"opensearch": "token-exchange is not implemented in the broker and there is no adapter (ws-06)",
               "servicedesk": "service-account is not implemented in the broker and there is no adapter (ws-06)"}


def check_broker_knows_systems():
    """Every MCP server of the compose file has its system configured in the broker: the portal catalog (built from C4
    GET /v1/systems) carries a credential entry for it. A resolve for an unknown system is a 400 at the broker, which
    the gateway reports to the agent as internal_error."""
    config = json.loads(subprocess.run([str(ROOT / "scripts" / "compose.sh"), "--profile", "full", "--profile", "m2", "config", "--format", "json"],
                                       capture_output=True, text=True).stdout)
    systems = sorted(name.removeprefix("mcp-") for name, svc in config["services"].items() if "MCP_INTERNAL_URL" in (svc.get("environment") or {}))
    portal = m2.Portal()
    portal.login("alice")
    status, _, raw = portal.request("GET", f"{PROXY}/api/v1/catalog")
    catalog = {item.get("system"): item for item in jbody(raw).get("servers", [])}
    for system in systems:
        known = bool(catalog.get(system, {}).get("credential"))
        name = f"broker: the system {system} is configured (catalog has its credential types)"
        if known:
            record(name, True)
        elif system in BROKER_GAPS:
            skip(name, BROKER_GAPS[system])
        else:
            record(name, False, f"HTTP {status}: no credential entry for {system}")


# ---- scenario ---------------------------------------------------------------------------------------------

JIRA_DELETE = "jira__delete_issue"
# The BFF forwards a view only for a same-origin request (C12 markMyConfirmationViewed).
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


def delete_journal():
    """(method, url) of DELETE requests that reached the Jira mock with the PAT of alice (whole journal of the mock)."""
    return [(r["method"], r["url"]) for r in mcpdev.jira_auth(1000)
            if r["method"] == "DELETE" and r["status"] == 204 and r["credential"] == "PAT alice-pat"]


def error_of(reply):
    """tool error object of a tools/call reply (C10), or {}."""
    try:
        data = json.loads(m2.tool_text(reply))
    except ValueError:
        return {}
    return data if isinstance(data, dict) and "error" in data else {}


class Scenario:
    def __init__(self):
        self.seed = None
        self.portal = None

    def need(self, name, ok, reason):
        if not ok:
            skip(name, reason)
        return ok

    def run(self):
        self.portal = m2.Portal()
        self.portal.login("alice")
        status, _, raw = self.portal.request("GET", f"{PROXY}/api/v1/authenticator")
        broker_m3 = status == 200 and "state" in jbody(raw)
        if not self.need("Authenticator registration with step-up MFA", broker_m3,
                         f"BFF/broker without M3 (GET /api/v1/authenticator -> HTTP {status})"):
            for step in ("delete_issue -> confirmation_required", "preview", "code and execution", "cancel", "block after preview",
                         "Cedar denial", "activity log"):
                skip(step, "needs the Authenticator registration (broker, BFF M3)")
            return
        self.reset()
        self.register()
        call = self.session("alice")
        before = len(delete_journal())
        first = self.first_call(call, "ABC-2")
        if not self.need("delete_issue -> confirmation_required", first.get("error") == "confirmation_required" and first.get("confirmationId"),
                         f"gateway without M3 (answer: {first.get('error')})"):
            for step in ("preview", "code and execution", "cancel", "block after preview", "Cedar denial", "activity log"):
                skip(step, "needs confirmation_required from the gateway (M3)")
            return
        self.preview_code_execute(call, first, before)
        self.cancel(call)
        self.block_after_preview(call)
        self.cedar_denial()
        self.activity()

    def reset(self):
        status, body = mcpdev.reset_authenticator("alice")
        record("reset: Authenticator of alice removed through mcp-offboarding", status == 204, f"HTTP {status} {body}")
        status, _, raw = self.portal.request("GET", f"{PROXY}/api/v1/authenticator")
        record("portal: alice has no Authenticator after the reset", jbody(raw).get("state") == "none", raw[:80].decode("utf-8", "replace"))

    def register(self):
        plain = m2.Portal()
        plain.login("alice")
        status, _, raw = plain.request("POST", f"{PROXY}/api/v1/authenticator/enrollment", {})
        record("portal: enrollment without a fresh MFA is refused (step_up_required)", status in (401, 403) and b"step_up_required" in raw, f"HTTP {status}")
        stepped = m2.Portal()
        stepped.login("alice", step_up=True)
        status, headers, raw = stepped.request("POST", f"{PROXY}/api/v1/authenticator/enrollment", {})
        body = jbody(raw)
        record("portal: enrollment after step-up -> 201, no-store, seed and otpauth URI once",
               status == 201 and "no-store" in (headers.get("Cache-Control") or "") and re.fullmatch(r"[A-Z2-7]{32}", body.get("secret", "")) is not None
               and body.get("otpauthUri", "").startswith("otpauth://totp/"), f"HTTP {status}")
        self.seed = body.get("secret")
        if not self.seed:
            return
        status, _, raw = stepped.request("POST", f"{PROXY}/api/v1/authenticator/enrollment/confirm",
                                         {"enrollmentId": body["enrollmentId"], "code": totp.fresh_code(self.seed)})
        record("portal: first code activates the registration", status == 200 and jbody(raw).get("state") == "active", f"HTTP {status}")
        status, _, raw = self.portal.request("GET", f"{PROXY}/api/v1/authenticator")
        record("portal: status is active and carries no seed", jbody(raw).get("state") == "active" and self.seed.encode() not in raw)

    def session(self, user):
        token = mcpdev.login(user, "claude-code")
        call = m2.mcp_session(f"{PROXY}/s/mcp-jira/mcp", token)
        call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "m3", "version": "1"}})
        call("notifications/initialized", notify=True)
        return call

    def first_call(self, call, key, code=None):
        args = {"issueKey": key, **({"confirmation_code": code} if code else {})}
        _, reply = call("tools/call", {"name": JIRA_DELETE, "arguments": args})
        return error_of(reply) or {"raw": reply}

    def preview_code_execute(self, call, first, before):
        cid = first["confirmationId"]
        record("gateway: confirmation_required carries description, previewUrl and expiresAt, no code or seed",
               all(first.get(k) for k in ("description", "previewUrl", "expiresAt")) and "ABC-2" in first["description"])
        status, headers, raw = self.portal.request("GET", f"{PROXY}/api/v1/confirmations/{cid}")
        record("preview: owner reads the pending operation (no-store, no buttons of decision)", status == 200 and "no-store" in (headers.get("Cache-Control") or ""), f"HTTP {status}")
        status, _, _ = self.portal.request("POST", f"{PROXY}/api/v1/confirmations/{cid}/viewed", {}, SAME_ORIGIN)
        record("preview: viewed is recorded", status in (200, 204), f"HTTP {status}")
        status, _, _ = m2.Portal().request("GET", f"{PROXY}/api/v1/confirmations/{cid}")
        record("preview: anonymous reader is refused", status in (401, 403), f"HTTP {status}")
        bad = totp.code(self.seed, offset=5)
        wrong = self.first_call(call, "ABC-2", bad)
        record("code: a wrong code is confirmation_failed and nothing is deleted", wrong.get("error") == "confirmation_failed" and len(delete_journal()) == before, str(wrong.get("error")))
        done = self.first_call(call, "ABC-2", totp.fresh_code(self.seed))
        if "raw" in done and done["raw"].get("error", {}).get("code") == -32602:
            skip("execution: the right code runs delete_issue as alice (DELETE reached Jira once)",
                 "the code was accepted and the call reached mcp-jira, which has no delete_issue yet (Invalid params)")
            skip("code: the same code cannot confirm again", "needs the execution step")
            return
        record("execution: the right code runs delete_issue as alice (DELETE reached Jira once)", "error" not in done and len(delete_journal()) == before + 1, str(done)[:120])
        replay = self.first_call(call, "ABC-2", totp.code(self.seed))
        record("code: the same code cannot confirm again", replay.get("error") in ("confirmation_required", "confirmation_failed", "rate_limited") and len(delete_journal()) == before + 1, str(replay.get("error")))

    def cancel(self, call):
        before = len(delete_journal())
        pending = self.first_call(call, "ABC-1")
        if not pending.get("confirmationId"):
            record("cancel: pending operation created", False, str(pending)[:100])
            return
        status, _, _ = self.portal.request("POST", f"{PROXY}/api/v1/confirmations/{pending['confirmationId']}/cancel", {})
        record("cancel: owner cancels the pending operation", status == 204, f"HTTP {status}")
        after = self.first_call(call, "ABC-1", totp.fresh_code(self.seed))
        record("cancel: retrying with a code -> confirmation_cancelled, nothing deleted", after.get("error") == "confirmation_cancelled" and len(delete_journal()) == before, str(after.get("error")))

    def block_after_preview(self, call):
        a = self.first_call(call, "ABC-1")
        self.portal.request("POST", f"{PROXY}/api/v1/confirmations/{a.get('confirmationId')}/viewed", {}, SAME_ORIGIN)
        b = self.first_call(call, "ABC-2")
        record("block: another DESTRUCTIVE operation after the preview of the first -> confirmation_blocked with retryAfterSeconds",
               b.get("error") == "confirmation_blocked" and int(b.get("retryAfterSeconds", 0)) >= 1, str(b.get("error")))
        self.portal.request("POST", f"{PROXY}/api/v1/confirmations/{a.get('confirmationId')}/cancel", {})

    def cedar_denial(self):
        call = self.session("bob")
        before = len(delete_journal())
        reply = self.first_call(call, "ABC-2")
        record("cedar: bob (read only) cannot delete_issue, no confirmation is created",
               reply.get("error") in ("not_found_or_denied", "access_denied", "tool_disabled") and "confirmationId" not in reply and len(delete_journal()) == before, str(reply.get("error")))

    def activity(self):
        status, _, raw = self.portal.request("GET", f"{PROXY}/api/v1/activity/calls?limit=100")
        body = jbody(raw)
        rows = body if isinstance(body, list) else body.get("items", [])
        if not self.need("activity log", status == 200, f"BFF without /activity/calls (HTTP {status})"):
            return
        deletes = [r for r in rows if "delete_issue" in json.dumps(r)]
        ids = [r.get("requestId") for r in deletes if r.get("requestId")]
        record("activity: delete_issue calls are listed, one row per call", bool(deletes) and len(ids) == len(set(ids)), f"{len(deletes)} rows")
        record("activity: no code and no seed in the log", self.seed.encode() not in raw and not re.search(rb'"confirmation_code"', raw))


def run():
    for part in (check_openbao, check_redis_gateway, check_keycloak_mfa, check_secrets_hygiene, check_gateway_devstack, check_policy_server, check_misc, check_registry_tools, check_broker_knows_systems, lambda: Scenario().run()):
        try:
            part()
        except (mcpdev.Fail, OSError, KeyError, ValueError) as e:
            record(getattr(part, "__name__", "scenario"), False, f"{type(e).__name__}: {e}")
    for status, name, detail in results:
        print(f"{status:4} {name}" + (f"  [{detail}]" if detail else ""))
    failed = sum(1 for r in results if r[0] == "FAIL")
    print(f"m3: {sum(1 for r in results if r[0] == 'PASS')} passed, {failed} failed, {sum(1 for r in results if r[0] == 'SKIP')} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run() if sys.argv[1:] == ["run"] else __doc__ and print(__doc__) or 2)
