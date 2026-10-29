"""Offboarding and registry registration of scripts/mcpdev.py against fake servers: a Keycloak admin API over http and a broker and
a gateway over mTLS (certificates of certs/, so `make certs` first). The fakes apply the rules of the contracts that matter
here: the purge needs CN=mcp-offboarding on both listeners, the registry PUT needs CN=mcp-registry-ci-<serverId> equal to the path
(CC-161). Run: make m4-test."""
import http.server
import json
import os
import ssl
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CERTS = ROOT / "certs"
sys.path.insert(0, str(ROOT / "scripts"))

SUB = "11111111-2222-3333-4444-555555555555"
GROUPS = [{"id": "g1", "name": "mcp-jira-read"}, {"id": "g2", "name": "mcp-client-claude-code"}]


class Recorder:
    def __init__(self):
        self.calls = []
        self.broker_status = 204


def make_handler(kind, recorder):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body=b""):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            if body:
                self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def client_cn(self):
            subject = dict(item[0] for item in self.connection.getpeercert()["subject"])
            return subject["commonName"]

        def handle_any(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            path = self.path
            if kind == "keycloak":
                recorder.calls.append(("idp", self.command, path))
                if path.startswith("/realms/master/protocol/openid-connect/token"):
                    return self.reply(200, json.dumps({"access_token": "admin-token"}).encode())
                if path.startswith("/admin/realms/corp/users?username=leaver"):
                    return self.reply(200, json.dumps([{"id": SUB}]).encode())
                if path.startswith("/admin/realms/corp/users?username="):
                    return self.reply(200, b"[]")
                if path == f"/admin/realms/corp/users/{SUB}/groups" and self.command == "GET":
                    return self.reply(200, json.dumps(GROUPS).encode())
                if self.command == "DELETE" and path.startswith(f"/admin/realms/corp/users/{SUB}/groups/"):
                    return self.reply(204)
                if path == f"/admin/realms/corp/users/{SUB}/logout":
                    return self.reply(204)
                return self.reply(404)
            cn = self.client_cn()
            recorder.calls.append((kind, self.command, path, cn))
            if kind == "broker" and path == f"/v1/admin/users/{SUB}/purge":
                return self.reply(recorder.broker_status if cn == "mcp-offboarding" else 403)
            if kind == "gateway" and path == f"/admin/v1/users/{SUB}/purge":
                return self.reply(204 if cn == "mcp-offboarding" else 403)
            if kind == "gateway" and path.startswith("/admin/v1/registry/servers/"):
                server = path.rsplit("/", 1)[1]
                return self.reply(200 if cn == f"mcp-registry-ci-{server}" else 403)
            return self.reply(404)

        do_GET = do_POST = do_PUT = do_DELETE = handle_any

    return Handler


def serve(kind, recorder, identity=None):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kind, recorder))
    if identity:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(CERTS / identity / "tls.crt", CERTS / identity / "tls.key")
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(CERTS / "ca" / "ca.crt")
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@unittest.skipUnless((CERTS / "legacy" / "mcp-registry-ci" / "tls.crt").exists(), "make certs first")
class OffboardRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recorder = Recorder()
        cls.keycloak = serve("keycloak", cls.recorder)
        cls.broker = serve("broker", cls.recorder, "mcp-credential-broker")
        cls.gateway = serve("gateway", cls.recorder, "mcp-gateway")
        os.environ.update({
            "KEYCLOAK_ADMIN_URL": f"http://127.0.0.1:{cls.keycloak.server_port}",
            "BROKER_URL": f"https://127.0.0.1:{cls.broker.server_port}",
            "GATEWAY_ADMIN_URL": f"https://127.0.0.1:{cls.gateway.server_port}",
            "MCP_BROKER_TLS_NAME": "mcp-credential-broker.internal",
            "MCP_GATEWAY_INTERNAL_TLS_NAME": "mcp-gateway.internal",
            "KC_BOOTSTRAP_ADMIN_USERNAME": "admin", "KC_BOOTSTRAP_ADMIN_PASSWORD": "unit-test",
        })
        import mcpdev
        cls.mcpdev = mcpdev

    @classmethod
    def tearDownClass(cls):
        for server in (cls.keycloak, cls.broker, cls.gateway):
            server.shutdown()

    def setUp(self):
        self.recorder.calls.clear()
        self.recorder.broker_status = 204

    def test_registry_put_uses_the_certificate_of_the_server(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json") as manifest:
            manifest.write('{"serverId": "mcp-jira"}')
            manifest.flush()
            self.assertEqual(self.mcpdev.registry_put("mcp-jira", manifest.name)[0], 200)
            self.assertEqual(self.mcpdev.registry_put("mcp-jira", manifest.name, "mcp-registry-ci-mcp-gitlab")[0], 403)
            self.assertEqual(self.mcpdev.registry_put("mcp-jira", manifest.name, "legacy/mcp-registry-ci")[0], 403)
            self.assertEqual(self.mcpdev.registry_put("mcp-opensearch", manifest.name, "mcp-offboarding")[0], 403)

    def test_offboard_order_and_repetition(self):
        steps = self.mcpdev.offboard("leaver", repeat_delay=0)
        self.assertTrue(all(ok for _, ok, _ in steps), steps)
        calls = [(c[0], c[1], c[2].split("/")[-1]) for c in self.recorder.calls
                 if not c[2].startswith("/realms/master") and "username=" not in c[2] and not (c[0] == "idp" and c[1] == "GET")]
        self.assertEqual(calls, [("idp", "DELETE", "g1"), ("idp", "DELETE", "g2"), ("idp", "POST", "logout"),
                                 ("broker", "POST", "purge"), ("gateway", "POST", "purge"),
                                 ("broker", "POST", "purge"), ("gateway", "POST", "purge")])
        self.assertEqual({c[3] for c in self.recorder.calls if c[0] in ("broker", "gateway")}, {"mcp-offboarding"})

    def test_a_failing_broker_does_not_stop_the_gateway_purge(self):
        self.recorder.broker_status = 503
        steps = self.mcpdev.offboard("leaver", repeat_delay=0)
        self.assertFalse(all(ok for _, ok, _ in steps))
        self.assertEqual(sum(1 for c in self.recorder.calls if c[0] == "gateway"), 2)

    def test_dry_run_changes_nothing(self):
        steps = self.mcpdev.offboard("leaver", dry_run=True)
        self.assertTrue(all(ok for _, ok, _ in steps))
        self.assertEqual([c for c in self.recorder.calls if c[0] in ("broker", "gateway") or c[1] in ("DELETE", "POST")
                          and "openid-connect" not in c[2]], [])

    def test_shared_test_users_are_refused(self):
        with self.assertRaises(self.mcpdev.Fail):
            self.mcpdev.offboard("alice")
        self.assertEqual(self.recorder.calls, [])

    def test_unknown_user_is_an_error(self):
        with self.assertRaises(self.mcpdev.Fail):
            self.mcpdev.offboard("nobody")


if __name__ == "__main__":
    unittest.main()
