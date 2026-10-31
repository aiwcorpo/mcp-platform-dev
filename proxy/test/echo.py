"""Stand-in upstream for the proxy tests: answers with what it received.

Query switches: echo_status=N, echo_cookie=1 (Set-Cookie), echo_csp=1 (own CSP and nosniff headers),
echo_rid=<v> (X-Request-Id in the response), echo_type=<mime> (Content-Type of the body).
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

NAME = os.environ["ECHO_NAME"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(fmt % args + "\n")

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        q = parse_qs(urlsplit(self.path).query)
        body = json.dumps({"upstream": NAME, "method": self.command, "uri": self.path,
                           "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(int(q.get("echo_status", ["200"])[0]))
        self.send_header("Content-Type", q.get("echo_type", ["application/json"])[0])
        self.send_header("Content-Length", str(len(body)))
        if "echo_cookie" in q:
            self.send_header("Set-Cookie", "__Host-session=abc; Path=/; Secure; HttpOnly")
        if "echo_csp" in q:
            self.send_header("Content-Security-Policy", "default-src *")
            self.send_header("X-Content-Type-Options", "sniff")
        if "echo_rid" in q:
            self.send_header("X-Request-Id", q["echo_rid"][0])
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _serve


ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("ECHO_PORT", "8080"))), Handler).serve_forever()
