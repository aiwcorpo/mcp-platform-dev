"""MCP server stand-in of the dev stack (DEV ONLY, not a product): mTLS listener, internal JWT check (C2), tools/list and tools/call
of a generated tool set, GET /mcp-manifest (C6). It exists to give the gateway something to list and call that the five real servers
cannot be: a server with more than 100 tools (toolset bulk, scenario G9) and a server whose manifest the test controls (toolset xh).

The contracts know five systems and the gateway refuses any other (SystemId and ServerId of mcp-contracts, ServerRegistry), so a
stand-in takes the identity of one of them: by default mcp-confluence, whose group mcp-confluence-read and policy rules already exist.
The real server of that identity must not run at the same time (same server id, same address).

  python3 mcp_stub.py serve              settings from the environment, see Settings.from_environment
  python3 mcp_stub.py manifest [--environment dev|production|none]   the manifest of the configured server on stdout

Standard library only. No data of any system is involved: tools/call answers a fixed text and the kind of credential that arrived
(never its value).
"""
import argparse
import hashlib
import http.client
import json
import os
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import internal_jwt

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MAX_BODY = 1 << 20
CREDENTIAL_TYPES = ("user-oauth", "user-secret", "token-exchange", "service-account")


def canonical(value):
    """RFC 8785 for the values used here (strings, integers, booleans, arrays, objects): sorted keys, no spaces, raw UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def definition_hash(definition, entry, system, credential):
    """definitionHash of C6 (mcp-contracts, DefinitionHash.java and test-vectors/definition-hash)."""
    flat = {"name": definition["name"], "description": definition["description"],
            "inputSchema": json.loads(json.dumps(definition["inputSchema"])),
            "resourceRefs": entry.get("resourceRefs", []), "system": system, "credential": credential}
    # The entry of an adapter tool has no risk and no action (CC-193): the flat object has no such keys.
    for key in ("risk", "action"):
        if key in entry:
            flat[key] = entry[key]
    if "title" in definition:
        flat["title"] = definition["title"]
    for optional in ("outputSchema", "annotations"):
        if optional in definition:
            flat[optional] = definition[optional]
    properties = flat["inputSchema"].get("properties", {})
    if "confirmation_code" in properties:
        del properties["confirmation_code"]
        flat["inputSchema"]["required"] = [r for r in flat["inputSchema"].get("required", []) if r != "confirmation_code"]
    return "sha256:" + hashlib.sha256(canonical(flat).encode()).hexdigest()


def no_argument_schema():
    return {"type": "object", "properties": {}, "additionalProperties": False}


def bulk_tools(count):
    tools = []
    for number in range(1, count + 1):
        name = f"read_item_{number:03d}"
        tools.append({"name": name, "risk": "READ_ONLY", "action": "read", "resourceRefs": [], "definition": {
            "name": name, "title": f"Read item {number:03d}",
            "description": f"Returns the fixed record {number:03d} of the bulk stand-in catalog.",
            "inputSchema": no_argument_schema(), "annotations": {"readOnlyHint": True}}})
    return tools


def xh_tools():
    schema = {"type": "object", "properties": {"jql": {"type": "string", "description": "Query text", "maxLength": 256}},
              "required": ["jql"], "additionalProperties": False}
    return [{"name": "search_issues", "risk": "READ_ONLY", "action": "read", "resourceRefs": [], "definition": {
        "name": "search_issues", "title": "Search issues", "description": "Returns a fixed answer for any query (stand-in).",
        "inputSchema": schema, "annotations": {"readOnlyHint": True}}}]


TOOLSETS = {"bulk": lambda settings: bulk_tools(settings.tool_count), "xh": lambda settings: xh_tools()}


@dataclass
class Settings:
    server_id: str = "mcp-confluence"
    system: str = "confluence"
    toolset: str = "bulk"
    tool_count: int = 250
    version: str = "1.0.0"
    environment: str = ""
    internal_url: str = "https://mcp-confluence.internal:8443/mcp"
    credential_required: bool = True
    credential_types: tuple = ("user-secret",)
    port: int = 8443
    cert: str = "/certs/tls.crt"
    key: str = "/certs/tls.key"
    ca: str = "/certs/ca.crt"
    client_cns: tuple = ("mcp-gateway",)
    jwks_uri: str = "https://mcp-gateway.internal:8443/.well-known/jwks.json"
    issuer: str = "https://mcp-gateway.internal"
    allow_test_keys: bool = False
    devstack: bool = False
    base_manifest: str = ""

    @classmethod
    def from_environment(cls, env=os.environ):
        def flag(name):
            return env.get(name, "false").lower() == "true"

        def listed(name, default):
            return tuple(item for item in env.get(name, default).split(",") if item)

        settings = cls(
            server_id=env.get("MCP_STUB_SERVER_ID", cls.server_id), system=env.get("MCP_STUB_SYSTEM", cls.system),
            toolset=env.get("MCP_STUB_TOOLSET", cls.toolset), tool_count=int(env.get("MCP_STUB_TOOL_COUNT", cls.tool_count)),
            version=env.get("MCP_STUB_VERSION", cls.version), environment=env.get("MCP_STUB_ENVIRONMENT", ""),
            internal_url=env.get("MCP_STUB_INTERNAL_URL", cls.internal_url), credential_required=flag("MCP_STUB_CREDENTIAL_REQUIRED"),
            credential_types=listed("MCP_STUB_CREDENTIAL_TYPES", ""), port=int(env.get("MCP_STUB_PORT", cls.port)),
            cert=env.get("MCP_STUB_CERT", cls.cert), key=env.get("MCP_STUB_KEY", cls.key), ca=env.get("MCP_STUB_CA", cls.ca),
            client_cns=listed("MCP_STUB_CLIENT_CNS", "mcp-gateway"), jwks_uri=env.get("MCP_GATEWAY_JWKS_URI", cls.jwks_uri),
            issuer=env.get("MCP_GATEWAY_ISSUER", cls.issuer), allow_test_keys=flag("MCP_STUB_ALLOW_TEST_KEYS"),
            devstack=flag("MCP_STUB_DEVSTACK"), base_manifest=env.get("MCP_STUB_BASE_MANIFEST", ""))
        settings.validate()
        return settings

    def validate(self):
        if self.toolset not in TOOLSETS:
            raise ValueError(f"MCP_STUB_TOOLSET must be one of {sorted(TOOLSETS)}")
        if not 1 <= self.tool_count <= 1000:
            raise ValueError("MCP_STUB_TOOL_COUNT must be 1..1000")
        if self.environment not in ("", "dev", "production"):
            raise ValueError("MCP_STUB_ENVIRONMENT must be empty, dev or production")
        if not set(self.credential_types) <= set(CREDENTIAL_TYPES):
            raise ValueError("MCP_STUB_CREDENTIAL_TYPES holds a type outside the contract")
        if self.credential_required and not self.credential_types:
            raise ValueError("a server that needs a credential names the types it accepts")
        if not self.internal_url.startswith("https://"):
            raise ValueError("MCP_STUB_INTERNAL_URL must be https")


def build_manifest(settings, environment=None):
    """The manifest of C6. `environment` overrides the setting (None keeps it, "none" omits the field).

    With a base manifest (MCP_STUB_BASE_MANIFEST, the build artifact of the real server) the stand-in IS that server plus its own tools:
    version, credential, resource types and the entries of the real tools are taken over unchanged, hashes included, so that a registry
    that approved them keeps its approvals while the stand-in runs and when the real manifest is registered again."""
    credential = {"required": settings.credential_required, "acceptedTypes": list(settings.credential_types)}
    base = {}
    if settings.base_manifest:
        with open(settings.base_manifest) as handle:
            base = json.load(handle)
        for field, wanted in (("serverId", settings.server_id), ("system", settings.system), ("internalUrl", settings.internal_url)):
            if base.get(field) != wanted:
                raise ValueError(f"the base manifest has {field} {base.get(field)!r}, the stand-in {wanted!r}")
        credential = base["credential"]
    own = TOOLSETS[settings.toolset](settings)
    for tool in own:
        tool["definitionHash"] = definition_hash(tool["definition"], tool, settings.system, credential)
    if base:
        clash = {t["name"] for t in base["tools"]} & {t["name"] for t in own}
        if clash:
            raise ValueError(f"tool names of the stand-in clash with the base manifest: {sorted(clash)[:3]}")
        own = base["tools"] + own
    manifest = {"serverId": settings.server_id, "version": base.get("version", settings.version), "system": settings.system,
                "internalUrl": settings.internal_url, "credential": credential, "resourceTypes": base.get("resourceTypes", []),
                "tools": own}
    chosen = settings.environment if environment is None else ("" if environment == "none" else environment)
    if chosen:
        manifest["environment"] = chosen
    return manifest


class JwksCache:
    """The gateway key set, fetched over https with the dev CA; refetched at most every 10 s when a kid is unknown."""

    def __init__(self, uri, ca, clock=time.monotonic):
        self.uri, self.ca, self.clock = uri, ca, clock
        self.keys = {"keys": []}
        self.fetched = None
        self.lock = threading.Lock()

    def current(self, kid=None):
        with self.lock:
            age = None if self.fetched is None else self.clock() - self.fetched
            known = any(k.get("kid") == kid for k in self.keys["keys"])
            if age is None or age > 300 or (not known and age > 10):
                self._fetch()
            return self.keys

    def _fetch(self):
        self.fetched = self.clock()
        context = ssl.create_default_context(cafile=self.ca)
        try:
            with urllib.request.urlopen(self.uri, timeout=5, context=context) as response:
                keys = json.loads(response.read(MAX_BODY))
            if isinstance(keys.get("keys"), list):
                self.keys = keys
        except (OSError, ValueError, http.client.HTTPException) as error:
            sys.stderr.write(f"jwks fetch failed: {type(error).__name__}\n")


class JtiLog:
    def __init__(self, clock=time.time):
        self.clock, self.seen, self.lock = clock, {}, threading.Lock()

    def __contains__(self, jti):
        with self.lock:
            return self.seen.get(jti, 0) > self.clock()

    def record(self, jti, expires):
        with self.lock:
            now = self.clock()
            self.seen = {k: v for k, v in self.seen.items() if v > now}
            self.seen[jti] = expires + 30


class Stub:
    """The protocol part, independent of the socket: answers (status, headers, body) for a request."""

    def __init__(self, settings, jwks, tls=None, clock=time.time):
        self.settings = settings
        self.jwks = jwks
        self.tls = tls
        self.clock = clock
        self.jtis = JtiLog(clock)
        self.verified = internal_jwt.VerifiedTokens()
        self.manifest = build_manifest(settings)
        self.own = {tool["name"] for tool in TOOLSETS[settings.toolset](settings)}
        self.tools = {tool["name"]: tool["definition"] for tool in self.manifest["tools"]}
        self.listing = [tool["definition"] for tool in self.manifest["tools"]]
        self.path = urllib.parse.urlparse(settings.internal_url).path or "/mcp"

    def authenticate(self, headers, request):
        header = headers.get("Authorization", "")
        token = header[7:] if header[:7].lower() == "bearer " else None
        kid = None
        if token:
            try:
                kid = json.loads(internal_jwt.b64url_decode(token.split(".")[0])).get("kid")
            except (ValueError, AttributeError, IndexError):
                kid = None
        claims = internal_jwt.verify(
            token, jwks=self.jwks.current(kid), issuer=self.settings.issuer, audience=self.settings.server_id, now=int(self.clock()),
            request=request, seen_jtis=self.jtis, allow_test_keys=self.settings.allow_test_keys, devstack=self.settings.devstack, verified=self.verified)
        self.jtis.record(claims["jti"], claims["exp"])
        return claims

    def handle(self, method, path, headers, body):
        route = urllib.parse.urlparse(path).path
        request_id = headers.get("X-Request-Id", "")
        if method == "GET" and route == "/mcp-manifest":
            return self._guarded(headers, {"kind": "http", "method": "GET", "path": "/mcp-manifest", "xRequestId": request_id},
                                 lambda claims: self._json(200, self.manifest))
        if route != self.path:
            return self._json(404, {"error": "not_found"})
        if method != "POST":
            return self._json(405, {"error": "method_not_allowed"}, {"Allow": "POST"})
        try:
            message = json.loads(body)
        except ValueError:
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}})
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        rpc_method = message["method"]
        request = {"kind": "jsonrpc", "method": rpc_method, "toolName": params.get("name"), "xRequestId": request_id}
        return self._guarded(headers, request, lambda claims: self._rpc(message, rpc_method, params, headers))

    def _guarded(self, headers, request, action):
        try:
            claims = self.authenticate(headers, request)
        except internal_jwt.Rejected as rejected:
            sys.stderr.write(f"rejected {request.get('method')} {request.get('toolName') or ''}: {rejected.reason}\n")
            return self._json(401, {"error": "unauthorized", "reason": rejected.reason}, {"WWW-Authenticate": "Bearer"})
        return action(claims)

    def _rpc(self, message, method, params, headers):
        ident = message.get("id")
        if method == "notifications/initialized":
            return 202, {}, b""
        if ident is None:
            return 202, {}, b""
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": self.settings.server_id, "version": self.settings.version}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.listing}
        elif method == "tools/call":
            name = params.get("name")
            if name not in self.tools:
                return self._json(200, {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "Unknown tool"}})
            if name not in self.own:
                text = json.dumps({"error": "stand_in", "message": "This tool of the real server is not served while the stand-in runs."})
                return self._json(200, {"jsonrpc": "2.0", "id": ident, "result": {"content": [{"type": "text", "text": text}], "isError": True}})
            kind = headers.get("X-MCP-Credential-Type", "none")
            result = {"content": [{"type": "text", "text": f"{name}: ok, credential type {kind}"}], "isError": False}
        else:
            return self._json(200, {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}})
        return self._json(200, {"jsonrpc": "2.0", "id": ident, "result": result})

    @staticmethod
    def _json(status, value, headers=None):
        return status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(value, separators=(",", ":")).encode()


def make_handler(stub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "mcp-dev-stub"
        sys_version = ""

        def setup(self):
            self.request.settimeout(10)
            self.request = stub.tls.wrap_socket(self.request, server_side=True)
            super().setup()

        def finish(self):
            try:
                super().finish()
            finally:
                self.request.close()

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def _client_allowed(self):
            subject = dict(item[0] for item in self.request.getpeercert().get("subject", ()))
            return subject.get("commonName") in stub.settings.client_cns

        def _serve(self):
            if not self._client_allowed():
                try:
                    self.rfile.read(max(0, min(int(self.headers.get("Content-Length") or 0), 65536)))
                except ValueError:
                    pass
                self.close_connection = True
                return self._send(403, {"Content-Type": "application/json"}, b'{"error":"client_not_allowed"}')
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self.close_connection = True
                return self._send(413, {"Content-Type": "application/json"}, b'{"error":"payload_too_large"}')
            body = self.rfile.read(length) if length else b""
            started = time.monotonic()
            status, headers, out = stub.handle(self.command, self.path, self.headers, body)
            sys.stderr.write("handled %s %s -> %s in %.0f ms\n" % (self.command, self.path, status, (time.monotonic() - started) * 1000))
            self._send(status, headers, out)

        def _send(self, status, headers, body):
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_DELETE = do_PUT = _serve

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # The default backlog is 5: a burst of lists opens dozens of connections at once, the excess is dropped and retried by the client's
    # TCP after 1 s and 3 s, which is where the multi-second answers came from.
    request_queue_size = 512

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if not isinstance(error, (ssl.SSLError, ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


def tls_context(settings):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(settings.cert, settings.key)
    context.load_verify_locations(settings.ca)
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def serve(settings):
    stub = Stub(settings, JwksCache(settings.jwks_uri, settings.ca), tls_context(settings))
    server = Server(("0.0.0.0", settings.port), make_handler(stub))
    sys.stderr.write(f"{settings.server_id} stand-in: {len(stub.tools)} tools, port {settings.port}\n")
    server.serve_forever()


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    printing = commands.add_parser("manifest")
    printing.add_argument("--environment", choices=["dev", "production", "none"])
    args = parser.parse_args(argv)
    settings = Settings.from_environment()
    if args.command == "manifest":
        print(json.dumps(build_manifest(settings, args.environment), indent=2))
        return 0
    serve(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
