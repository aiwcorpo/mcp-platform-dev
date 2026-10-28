"""Runs one system mock. MOCK_SYSTEM selects the module (gitlab, confluence, servicedesk, opensearch).

Environment: MOCK_SYSTEM, MOCK_PORT (default 8080), MOCK_ADMIN_PASSWORD (Basic auth for /__mock/journal).
GET /__mock/health is open; GET and DELETE /__mock/journal need Basic auth admin:$MOCK_ADMIN_PASSWORD.
"""
import hmac
import importlib
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import common

MAX_BODY = 1 << 20


def make_handler(system, journal, admin_password):
    module = importlib.import_module(system)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "mcp-dev-mock"
        sys_version = ""

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def _admin_ok(self, req):
            creds = req.basic()
            return bool(admin_password) and creds is not None and creds[0] == "admin" and \
                hmac.compare_digest(creds[1].encode(), admin_password.encode())

        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(common.json_response(413, {"error": "payload_too_large"}), close=True)
                return
            body = self.rfile.read(length) if length else b""
            req = common.Request(self.command, self.path, dict(self.headers.items()), body)
            if req.raw_path == "/__mock/health":
                resp = common.json_response(200, {"status": "UP", "system": system})
            elif req.raw_path == "/__mock/journal":
                if not self._admin_ok(req):
                    resp = common.json_response(401, {"error": "unauthorized"},
                                                {"WWW-Authenticate": 'Basic realm="mock"'})
                elif req.method == "DELETE":
                    journal.clear()
                    resp = common.Response(204, b"", content_type=None)
                else:
                    resp = common.json_response(200, journal.entries())
            else:
                try:
                    resp = module.handle(req) or common.json_response(404, {"error": "not_found"})
                except Exception as exc:  # a handler bug must not kill the connection thread silently
                    sys.stderr.write("handler error: %r\n" % (exc,))
                    resp = common.json_response(500, {"error": "mock_error"})
                journal.record(req, resp.status)
            self._send(resp, head=req.method == "HEAD")

        def _send(self, resp, head=False, close=False):
            self.send_response(resp.status)
            for name, value in resp.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(resp.body)))
            if close:
                self.send_header("Connection", "close")
            self.end_headers()
            if not head and resp.status not in (204, 304):
                self.wfile.write(resp.body)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _serve

    return Handler


def main():
    system = os.environ["MOCK_SYSTEM"]
    port = int(os.environ.get("MOCK_PORT", "8080"))
    handler = make_handler(system, common.Journal(), os.environ.get("MOCK_ADMIN_PASSWORD", ""))
    ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
