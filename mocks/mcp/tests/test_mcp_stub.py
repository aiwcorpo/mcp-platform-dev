import calendar
import glob
import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import internal_jwt  # noqa: E402
import mcp_stub  # noqa: E402

CONTRACTS = next((p for p in (os.environ.get("MCP_CONTRACTS_DIR", ""), os.path.join(os.path.dirname(os.path.abspath(__file__)), *[".."] * 4, "mcp-contracts"))
                  if p and os.path.isdir(os.path.join(p, "test-vectors", "jwt"))), None)
SCALAR = 0x1F2E3D4C5B6A79880123456789ABCDEF0123456789ABCDEF0123456789ABCDE
NOW = 1_800_000_000


def read_json(path):
    with open(path) as handle:
        return json.load(handle)


def public_jwk(kid):
    x, y = internal_jwt._multiply(SCALAR, internal_jwt.G)
    return {"kty": "EC", "crv": "P-256", "kid": kid, "alg": "ES256", "use": "sig",
            "x": internal_jwt.b64url_encode(x.to_bytes(32, "big")), "y": internal_jwt.b64url_encode(y.to_bytes(32, "big"))}


def token(claims, kid="stack-c2-1", alg="ES256"):
    head = internal_jwt.b64url_encode(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    body = internal_jwt.b64url_encode(json.dumps(claims).encode())
    signing_input = (head + "." + body).encode()
    return head + "." + body + "." + internal_jwt.b64url_encode(internal_jwt.sign_es256(signing_input, SCALAR, 0x123456789ABCDEF))


def user_claims(audience, jti, tool=None, rid="rid-1"):
    claims = {"iss": "https://mcp-gateway.internal", "aud": audience, "sub": "user-1", "usr": "alice", "grp": ["mcp-confluence-read"],
              "azp": "claude-code", "rid": rid, "prv": "20261026120000-a1b2c3d", "iat": NOW, "exp": NOW + 30, "jti": jti}
    if tool:
        claims["tool"] = tool
    return claims


def service_claims(audience, jti, rid="rid-1"):
    return {"iss": "https://mcp-gateway.internal", "aud": audience, "sub": "system:mcp-gateway", "grp": [], "rid": rid,
            "prv": "20261026120000-a1b2c3d", "iat": NOW, "exp": NOW + 30, "jti": jti}


class FixedKeys:
    def current(self, kid=None):
        return {"keys": [public_jwk("stack-c2-1")]}


def make_stub(**changes):
    settings = mcp_stub.Settings(**changes)
    settings.validate()
    return mcp_stub.Stub(settings, FixedKeys(), clock=lambda: NOW + 1)


def call(stub, method, path, headers, body=b""):
    status, out_headers, out = stub.handle(method, path, headers, body)
    return status, (json.loads(out) if out else None)


def rpc(stub, bearer, method, params=None, rid="rid-1", ident=1):
    body = json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}).encode()
    return call(stub, "POST", "/mcp", {"Authorization": "Bearer " + bearer, "X-Request-Id": rid}, body)


class DefinitionHashTest(unittest.TestCase):
    @unittest.skipIf(CONTRACTS is None, "mcp-contracts not next to this repository (MCP_CONTRACTS_DIR)")
    def test_vectors_of_the_contracts(self):
        files = sorted(glob.glob(os.path.join(CONTRACTS, "test-vectors", "definition-hash", "*.json")))
        self.assertGreater(len(files), 5)
        for path in files:
            vector = read_json(path)
            source = vector["input"]
            entry = dict(source["manifestEntry"])
            got = mcp_stub.definition_hash(source["tool"], entry, source["server"]["system"], source["server"]["credential"])
            self.assertEqual(vector["expected"], got, os.path.basename(path))

    def test_changing_the_risk_changes_the_hash(self):
        manifest = mcp_stub.build_manifest(mcp_stub.Settings())
        tool = manifest["tools"][0]
        changed = mcp_stub.definition_hash(tool["definition"], {**tool, "risk": "UPDATE"}, "confluence", manifest["credential"])
        self.assertNotEqual(tool["definitionHash"], changed)


class ManifestTest(unittest.TestCase):
    def test_bulk_manifest_has_250_read_only_tools_in_the_shape_of_c6(self):
        manifest = mcp_stub.build_manifest(mcp_stub.Settings())
        self.assertEqual("mcp-confluence", manifest["serverId"])
        self.assertEqual(250, len(manifest["tools"]))
        self.assertEqual(250, len({t["name"] for t in manifest["tools"]}))
        self.assertEqual({"READ_ONLY"}, {t["risk"] for t in manifest["tools"]})
        self.assertEqual({"name", "risk", "action", "resourceRefs", "definition", "definitionHash"}, set(manifest["tools"][0]))
        self.assertEqual({"required": True, "acceptedTypes": ["user-secret"]}, manifest["credential"])
        self.assertNotIn("environment", manifest)

    def test_environment_is_set_from_settings_or_per_call(self):
        settings = mcp_stub.Settings(toolset="xh", server_id="mcp-jira", system="jira", environment="dev", credential_required=True,
                                     credential_types=("user-secret",), internal_url="https://mcp-jira.internal:8443/mcp")
        self.assertEqual("dev", mcp_stub.build_manifest(settings)["environment"])
        self.assertNotIn("environment", mcp_stub.build_manifest(settings, "none"))
        self.assertEqual("production", mcp_stub.build_manifest(settings, "production")["environment"])

    def test_settings_refuse_nonsense(self):
        for changes in ({"toolset": "nope"}, {"tool_count": 0}, {"environment": "staging"}, {"credential_types": ()},
                        {"credential_types": ("password",)}, {"internal_url": "http://x/mcp"}):
            with self.assertRaises(ValueError, msg=str(changes)):
                mcp_stub.Settings(**changes).validate()

    def test_input_schemas_are_closed_and_strings_bounded(self):
        for tool in mcp_stub.build_manifest(mcp_stub.Settings(toolset="xh"))["tools"]:
            schema = tool["definition"]["inputSchema"]
            self.assertIs(False, schema["additionalProperties"])
            for prop in schema["properties"].values():
                self.assertIn("maxLength", prop)


class BaseManifestTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        credential = {"required": True, "acceptedTypes": ["user-secret"]}
        entry = {"name": "get_page", "risk": "READ_ONLY", "action": "read", "resourceRefs": [{"argument": "pageId", "type": "Confluence::Page"}],
                 "definition": {"name": "get_page", "title": "Get page", "description": "Real tool.", "annotations": {"readOnlyHint": True},
                                "inputSchema": {"type": "object", "properties": {"pageId": {"type": "string", "maxLength": 20}},
                                                "required": ["pageId"], "additionalProperties": False}}}
        entry["definitionHash"] = mcp_stub.definition_hash(entry["definition"], entry, "confluence", credential)
        self.base = {"serverId": "mcp-confluence", "version": "0.1.0-SNAPSHOT", "system": "confluence",
                     "internalUrl": "https://mcp-confluence.internal:8443/mcp", "credential": credential,
                     "resourceTypes": [{"type": "Confluence::Page", "attributes": ["id"]}], "tools": [entry]}
        self.path = os.path.join(self.dir, "base.json")
        with open(self.path, "w") as handle:
            json.dump(self.base, handle)

    def test_real_tools_stay_unchanged_in_front_of_the_stand_in_tools(self):
        manifest = mcp_stub.build_manifest(mcp_stub.Settings(base_manifest=self.path, tool_count=3))
        self.assertEqual(["get_page", "read_item_001", "read_item_002", "read_item_003"], [t["name"] for t in manifest["tools"]])
        self.assertEqual(self.base["tools"][0], manifest["tools"][0])
        self.assertEqual("0.1.0-SNAPSHOT", manifest["version"])
        self.assertEqual(self.base["resourceTypes"], manifest["resourceTypes"])

    def test_every_hash_is_recomputable_from_the_credential_of_the_manifest(self):
        for settings in (mcp_stub.Settings(base_manifest=self.path, tool_count=3, credential_required=False, credential_types=()),
                         mcp_stub.Settings(base_manifest=self.path, tool_count=3)):
            manifest = mcp_stub.build_manifest(settings)
            for tool in manifest["tools"]:
                self.assertEqual(tool["definitionHash"],
                                 mcp_stub.definition_hash(tool["definition"], tool, manifest["system"], manifest["credential"]), tool["name"])

    def test_a_real_tool_is_not_answered_with_a_success(self):
        stub = make_stub(base_manifest=self.path, tool_count=2)
        _, body = rpc(stub, token(user_claims("mcp-confluence", "j1", tool="get_page")), "tools/call", {"name": "get_page"})
        self.assertIs(True, body["result"]["isError"])
        _, body = rpc(stub, token(user_claims("mcp-confluence", "j2", tool="read_item_001")), "tools/call", {"name": "read_item_001"})
        self.assertIs(False, body["result"]["isError"])

    def test_the_stand_in_lists_the_definitions_of_its_manifest(self):
        stub = make_stub(base_manifest=self.path, tool_count=2)
        _, body = rpc(stub, token(service_claims("mcp-confluence", "j1")), "tools/list")
        self.assertEqual([t["definition"] for t in stub.manifest["tools"]], body["result"]["tools"])

    def test_a_base_manifest_of_another_server_is_refused(self):
        self.base["serverId"] = "mcp-jira"
        with open(self.path, "w") as handle:
            json.dump(self.base, handle)
        with self.assertRaises(ValueError):
            mcp_stub.build_manifest(mcp_stub.Settings(base_manifest=self.path))

    def test_clashing_tool_names_are_refused(self):
        self.base["tools"][0]["name"] = "read_item_001"
        with open(self.path, "w") as handle:
            json.dump(self.base, handle)
        with self.assertRaises(ValueError):
            mcp_stub.build_manifest(mcp_stub.Settings(base_manifest=self.path))


class ProtocolTest(unittest.TestCase):
    def test_gateway_sequence_initialize_list_call(self):
        stub = make_stub()
        status, body = rpc(stub, token(user_claims("mcp-confluence", "j1")), "initialize", {"protocolVersion": "2025-11-25"})
        self.assertEqual((200, "2025-11-25"), (status, body["result"]["protocolVersion"]))
        status, _ = call(stub, "POST", "/mcp", {"Authorization": "Bearer " + token(user_claims("mcp-confluence", "j2")), "X-Request-Id": "rid-1"},
                         json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode())
        self.assertEqual(202, status)
        status, body = rpc(stub, token(service_claims("mcp-confluence", "j3")), "tools/list")
        self.assertEqual(250, len(body["result"]["tools"]))
        self.assertNotIn("nextCursor", body["result"])
        status, body = rpc(stub, token(user_claims("mcp-confluence", "j4", tool="read_item_007")), "tools/call", {"name": "read_item_007", "arguments": {}})
        self.assertIn("read_item_007: ok", body["result"]["content"][0]["text"])

    def test_listed_definitions_are_the_ones_of_the_manifest(self):
        stub = make_stub()
        _, body = rpc(stub, token(service_claims("mcp-confluence", "j1")), "tools/list")
        self.assertEqual([t["definition"] for t in stub.manifest["tools"]], body["result"]["tools"])

    def test_manifest_needs_a_token_and_serves_the_same_document(self):
        stub = make_stub()
        status, body = call(stub, "GET", "/mcp-manifest", {"X-Request-Id": "rid-1"})
        self.assertEqual((401, "missing_token"), (status, body["reason"]))
        status, body = call(stub, "GET", "/mcp-manifest", {"Authorization": "Bearer " + token(service_claims("mcp-confluence", "j9")), "X-Request-Id": "rid-1"})
        self.assertEqual((200, stub.manifest), (status, body))

    def test_service_token_cannot_call_a_tool(self):
        stub = make_stub()
        status, body = rpc(stub, token(service_claims("mcp-confluence", "j1")), "tools/call", {"name": "read_item_001"})
        self.assertEqual((401, "service_token_not_allowed"), (status, body["reason"]))

    def test_tool_claim_must_match_the_called_tool(self):
        stub = make_stub()
        status, body = rpc(stub, token(user_claims("mcp-confluence", "j1", tool="read_item_001")), "tools/call", {"name": "read_item_002"})
        self.assertEqual((401, "tool_mismatch"), (status, body["reason"]))

    def test_replayed_token_is_refused(self):
        stub = make_stub()
        bearer = token(service_claims("mcp-confluence", "j1"))
        self.assertEqual(200, rpc(stub, bearer, "ping")[0])
        status, body = rpc(stub, bearer, "ping")
        self.assertEqual((401, "replayed_jti"), (status, body["reason"]))

    def test_token_for_another_server_and_unknown_key_are_refused(self):
        stub = make_stub()
        self.assertEqual("wrong_audience", rpc(stub, token(service_claims("mcp-jira", "j1")), "ping")[1]["reason"])
        self.assertEqual("unknown_kid", rpc(stub, token(service_claims("mcp-confluence", "j2"), kid="other"), "ping")[1]["reason"])

    def test_dev_and_test_keys_are_refused_unless_configured(self):
        self.assertEqual("test_key_not_allowed", rpc(make_stub(), token(service_claims("mcp-confluence", "j1"), kid="test-gw"), "ping")[1]["reason"])

    def test_unknown_tool_and_method_are_json_rpc_errors(self):
        stub = make_stub()
        _, body = rpc(stub, token(user_claims("mcp-confluence", "j1", tool="read_item_999")), "tools/call", {"name": "read_item_999"})
        self.assertEqual(-32602, body["error"]["code"])
        _, body = rpc(stub, token(user_claims("mcp-confluence", "j2")), "resources/list")
        self.assertEqual(-32601, body["error"]["code"])

    def test_garbage_body_is_a_parse_error_and_other_paths_are_404(self):
        stub = make_stub()
        self.assertEqual(-32700, call(stub, "POST", "/mcp", {}, b"{")[1]["error"]["code"])
        self.assertEqual(404, call(stub, "GET", "/elsewhere", {})[0])
        self.assertEqual(405, call(stub, "GET", "/mcp", {})[0])


class VectorTest(unittest.TestCase):
    @unittest.skipIf(CONTRACTS is None, "mcp-contracts not next to this repository (MCP_CONTRACTS_DIR)")
    def test_jwt_vectors_of_the_contracts(self):
        files = sorted(glob.glob(os.path.join(CONTRACTS, "test-vectors", "jwt", "*.json")))
        self.assertGreater(len(files), 40)
        for path in files:
            vector = read_json(path)
            verifier = vector["verifier"]
            jwks = read_json(os.path.join(CONTRACTS, "test-vectors", verifier["jwks"]))
            now = calendar.timegm(time.strptime(verifier["now"], "%Y-%m-%dT%H:%M:%SZ"))
            try:
                internal_jwt.verify(vector["token"], jwks=jwks, issuer=verifier["issuer"], audience=verifier["audience"], now=now,
                                    request=vector["request"], seen_jtis=set(verifier["seenJtis"]), skew=verifier["clockSkewSeconds"],
                                    max_lifetime=verifier["maxLifetimeSeconds"], allow_test_keys=verifier["allowTestKeys"],
                                    devstack=verifier.get("devstackProfile", False))
                outcome = (True, None)
            except internal_jwt.Rejected as rejected:
                outcome = (False, rejected.reason)
            self.assertEqual((vector["expected"]["valid"], vector["expected"].get("reason")), outcome, os.path.basename(path))


@unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
class TlsTest(unittest.TestCase):
    """The listener over a real socket: client certificate required, CN checked, token checked."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()

        def openssl(*args):
            subprocess.run(["openssl", *args], cwd=cls.dir, check=True, capture_output=True)

        openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", "ca.key", "-out", "ca.crt",
                "-subj", "/CN=test ca", "-days", "2", "-addext", "basicConstraints=critical,CA:TRUE",
                "-addext", "keyUsage=critical,keyCertSign")
        for name in ("server", "gateway", "other"):
            openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr",
                    "-subj", "/CN=" + {"server": "mcp-confluence", "gateway": "mcp-gateway", "other": "somebody"}[name])
            with open(os.path.join(cls.dir, f"{name}.ext"), "w") as ext:
                ext.write("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                          "extendedKeyUsage=serverAuth,clientAuth\nsubjectAltName=DNS:localhost\n")
            openssl("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-days", "2", "-out",
                    f"{name}.crt", "-extfile", f"{name}.ext")
        settings = mcp_stub.Settings(port=0, cert=f"{cls.dir}/server.crt", key=f"{cls.dir}/server.key", ca=f"{cls.dir}/ca.crt")
        cls.stub = mcp_stub.Stub(settings, FixedKeys(), mcp_stub.tls_context(settings), clock=lambda: NOW + 1)
        cls.server = mcp_stub.Server(("127.0.0.1", 0), mcp_stub.make_handler(cls.stub))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.dir)

    def connection(self, client):
        context = ssl.create_default_context(cafile=f"{self.dir}/ca.crt")
        if client:
            context.load_cert_chain(f"{self.dir}/{client}.crt", f"{self.dir}/{client}.key")
        return http.client.HTTPSConnection("localhost", self.port, context=context, timeout=5)

    def post_ping(self, client, jti):
        conn = self.connection(client)
        try:
            conn.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                         {"Authorization": "Bearer " + token(service_claims("mcp-confluence", jti)), "X-Request-Id": "rid-1", "Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_gateway_certificate_and_token_are_served(self):
        status, body = self.post_ping("gateway", "t1")
        self.assertEqual((200, {"jsonrpc": "2.0", "id": 1, "result": {}}), (status, json.loads(body)))

    def test_another_certificate_of_the_same_ca_is_refused(self):
        status, _ = self.post_ping("other", "t2")
        self.assertEqual(403, status)

    def test_a_negative_content_length_is_refused(self):
        conn = self.connection("gateway")
        try:
            conn.request("POST", "/mcp", b"", {"Content-Length": "-1"})
            self.assertEqual(413, conn.getresponse().status)
        finally:
            conn.close()

    def test_no_client_certificate_fails_the_handshake(self):
        with self.assertRaises((ssl.SSLError, ConnectionError)):
            self.post_ping(None, "t3")


if __name__ == "__main__":
    unittest.main()
