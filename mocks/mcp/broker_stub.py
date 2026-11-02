"""Credential broker stand-in for the e2e-prod profile (DEV ONLY, not a product). It answers the two calls a gateway makes outside
a user's request, GET /v1/systems and POST /v1/credentials/resolve (C4), with a credential whose type is set from outside, so a test
can make the broker return a type the system does not accept (CC-166). Authenticator and OAuth endpoints do not exist here.

The type is changed on a separate plain-http listener, protected by a bearer token (BROKER_STUB_ADMIN_TOKEN):
  GET  /type                      the type in force and the number of resolve calls
  PUT  /type  {"type": "..."}     any string up to 64 characters (a value outside the four of the contract is allowed on purpose)
The credential value is a fixed text, never a secret. The listener on the mTLS port needs a client certificate of the gateway
and an internal JWT (C2) with aud mcp-credential-broker.

  python3 broker_stub.py
Environment: BROKER_STUB_PORT (8443), BROKER_STUB_ADMIN_PORT (8080), BROKER_STUB_ADMIN_TOKEN (required, 16+ characters),
BROKER_STUB_TYPE (user-secret), BROKER_STUB_SYSTEM (jira), BROKER_STUB_SUPPORTED_TYPES (user-secret), MCP_STUB_CERT, MCP_STUB_KEY,
MCP_STUB_CA, MCP_STUB_CLIENT_CNS (mcp-gateway), MCP_GATEWAY_JWKS_URI, MCP_GATEWAY_ISSUER.
"""
import hmac
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import internal_jwt
import mcp_stub

AUDIENCE = "mcp-credential-broker"
MAX_TYPE = 64


class BrokerStub:
    def __init__(self, settings, jwks, admin_token, tls=None, system="jira", supported=("user-secret",), credential_type="user-secret",
                 clock=time.time):
        self.settings, self.jwks, self.tls, self.clock = settings, jwks, tls, clock
        self.admin_token = admin_token
        self.system, self.supported = system, list(supported)
        self.credential_type = credential_type
        self.calls = 0
        self.jtis = mcp_stub.JtiLog(clock)
        self.verified = internal_jwt.VerifiedTokens()
        self.lock = threading.Lock()

    def _authenticate(self, headers, request):
        header = headers.get("Authorization", "")
        bearer = header[7:] if header[:7].lower() == "bearer " else None
        kid = None
        if bearer:
            try:
                kid = json.loads(internal_jwt.b64url_decode(bearer.split(".")[0])).get("kid")
            except (ValueError, AttributeError, IndexError):
                kid = None
        claims = internal_jwt.verify(
            bearer, jwks=self.jwks.current(kid), issuer=self.settings.issuer, audience=AUDIENCE, now=int(self.clock()), request=request,
            seen_jtis=self.jtis, allow_test_keys=self.settings.allow_test_keys, devstack=self.settings.devstack, verified=self.verified)
        self.jtis.record(claims["jti"], claims["exp"])

    def handle(self, method, path, headers, body):
        route = urllib.parse.urlparse(path).path
        request_id = headers.get("X-Request-Id", "")
        if (method, route) not in {("GET", "/v1/systems"), ("POST", "/v1/credentials/resolve")}:
            return self._json(404, {"error": "not_found"})
        try:
            self._authenticate(headers, {"kind": "http", "method": method, "path": route, "xRequestId": request_id})
        except internal_jwt.Rejected as rejected:
            sys.stderr.write(f"rejected {method} {route}: {rejected.reason}\n")
            return self._json(401, {"error": "unauthorized", "reason": rejected.reason})
        if route == "/v1/systems":
            return self._json(200, [{"system": self.system, "supportedTypes": self.supported, "defaultType": self.supported[0]}])
        try:
            wanted = json.loads(body).get("system")
        except (ValueError, AttributeError):
            return self._json(400, {"error": "invalid_request"})
        if wanted != self.system:
            return self._json(422, {"error": "system_not_configured"})
        with self.lock:
            self.calls += 1
            issued = self.credential_type
        return self._json(200, {"scheme": "Token", "value": "stand-in-credential", "type": issued, "cacheTtlSeconds": 0})

    def admin(self, method, path, headers, body):
        supplied = headers.get("Authorization", "")
        expected = "Bearer " + self.admin_token
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return self._json(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
        if urllib.parse.urlparse(path).path != "/type":
            return self._json(404, {"error": "not_found"})
        if method == "PUT":
            try:
                value = json.loads(body)["type"]
            except (ValueError, KeyError, TypeError):
                return self._json(400, {"error": "invalid_request"})
            if not isinstance(value, str) or not 0 < len(value) <= MAX_TYPE:
                return self._json(400, {"error": "invalid_request"})
            with self.lock:
                self.credential_type = value
        elif method != "GET":
            return self._json(405, {"error": "method_not_allowed"})
        with self.lock:
            return self._json(200, {"type": self.credential_type, "resolveCalls": self.calls})

    @staticmethod
    def _json(status, value, headers=None):
        return status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(value).encode()


def admin_handler(stub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "mcp-dev-stub"
        sys_version = ""

        def log_message(self, fmt, *args):
            sys.stderr.write("admin %s %s\n" % (self.address_string(), fmt % args))

        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if 0 < length <= 4096 else b""
            status, headers, out = stub.admin(self.command, self.path, self.headers, body)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        do_GET = do_PUT = do_POST = do_DELETE = _serve

    return Handler


def main():
    env = os.environ
    token = env.get("BROKER_STUB_ADMIN_TOKEN", "")
    if len(token) < 16:
        sys.exit("BROKER_STUB_ADMIN_TOKEN must be at least 16 characters")
    settings = mcp_stub.Settings.from_environment({**env, "MCP_STUB_PORT": env.get("BROKER_STUB_PORT", "8443")})
    stub = BrokerStub(settings, mcp_stub.JwksCache(settings.jwks_uri, settings.ca), token, mcp_stub.tls_context(settings),
                      system=env.get("BROKER_STUB_SYSTEM", "jira"),
                      supported=tuple(t for t in env.get("BROKER_STUB_SUPPORTED_TYPES", "user-secret").split(",") if t),
                      credential_type=env.get("BROKER_STUB_TYPE", "user-secret"))
    admin = ThreadingHTTPServer(("0.0.0.0", int(env.get("BROKER_STUB_ADMIN_PORT", "8080"))), admin_handler(stub))
    admin.daemon_threads = True
    threading.Thread(target=admin.serve_forever, daemon=True).start()
    server = mcp_stub.Server(("0.0.0.0", settings.port), mcp_stub.make_handler(stub))
    sys.stderr.write(f"broker stand-in: system {stub.system}, type {stub.credential_type}, port {settings.port}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
