"""Shared pieces of the system mocks (GitLab, Confluence, Service Desk, OpenSearch).

Every mock is one process of server.py with MOCK_SYSTEM=<module name>; the module exposes
handle(req) -> Response | None (None = 404 in the system's own error shape is up to the module,
the server answers a plain JSON 404 otherwise).
"""
import base64
import json
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

# Same people and logins as the Keycloak realm and the Jira mock (C4.1: preferred_username == login).
USERS = {
    "alice": {"login": "alice", "name": "Alice Tester", "email": "alice@corp.test"},
    "bob": {"login": "bob", "name": "Bob Reader", "email": "bob@corp.test"},
    "carol": {"login": "carol", "name": "Carol Admin", "email": "carol@corp.test"},
    "mallory": {"login": "mallory", "name": "Mallory Nogroups", "email": "mallory@corp.test"},
    "someone-else": {"login": "someone-else", "name": "Someone Else", "email": "someone-else@corp.test"},
}

# Fixed test PATs: <login>-pat. They open nothing outside the mocks.
PATS = {f"{login}-pat": login for login in ("alice", "bob", "carol", "mallory")}


class Request:
    def __init__(self, method, target, headers, body):
        parts = urlsplit(target)
        self.method = method
        self.raw_path = parts.path
        self.path = unquote(parts.path)
        # Segments of the raw path, each decoded on its own: "projects/team-a%2Fapp" -> ["projects", "team-a/app"].
        self.segments = [unquote(s) for s in parts.path.split("/") if s != ""]
        self.query = parse_qs(parts.query, keep_blank_values=True)
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def param(self, name, default=None):
        values = self.query.get(name)
        return values[0] if values else default

    def json(self):
        """Parsed JSON body, or None when the body is empty or not JSON."""
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    def form(self):
        return {k: v[0] for k, v in parse_qs(self.body.decode("utf-8", "replace"), keep_blank_values=True).items()}

    def bearer(self):
        value = self.header("authorization", "")
        if value[:7].lower() == "bearer ":
            return value[7:].strip()
        return None

    def basic(self):
        value = self.header("authorization", "")
        if value[:6].lower() == "basic ":
            try:
                user, _, password = base64.b64decode(value[6:]).decode().partition(":")
                return user, password
            except ValueError:
                return None
        return None


class Response:
    def __init__(self, status=200, body=b"", headers=None, content_type="application/json"):
        self.status = status
        self.body = body
        self.headers = dict(headers or {})
        if content_type and "Content-Type" not in self.headers:
            self.headers["Content-Type"] = content_type


def json_response(status, obj, headers=None):
    return Response(status, json.dumps(obj).encode(), headers, "application/json;charset=UTF-8")


def text_response(status, text, content_type="text/plain;charset=UTF-8", headers=None):
    return Response(status, text.encode(), headers, content_type)


def html_response(status, html, headers=None):
    return Response(status, html.encode(), headers, "text/html;charset=UTF-8")


def jwt_claims(token):
    """Payload of a JWT without checking the signature (the mocks only look at aud/iss/preferred_username)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else None
    except (IndexError, ValueError):
        return None


class Journal:
    """What credentials reached the mock, never their values: a known PAT is recorded by name, a JWT as 'jwt'."""

    def __init__(self, limit=1000):
        self._lock = threading.Lock()
        self._entries = []
        self._limit = limit

    def record(self, req, status):
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "method": req.method,
                 "path": req.raw_path, "status": status, "auth": describe_auth(req)}
        with self._lock:
            self._entries.append(entry)
            del self._entries[:-self._limit]

    def entries(self):
        with self._lock:
            return list(self._entries)

    def clear(self):
        with self._lock:
            self._entries.clear()


OAUTH_TOKEN_PREFIX = "glo-"


def describe_auth(req):
    value = req.header("authorization")
    if not value:
        private = req.header("private-token")
        if private:
            return "pat:" + private if private in PATS else "private-token:unknown"
        return "none"
    token = req.bearer()
    if token is None:
        return "basic" if value[:6].lower() == "basic " else "other"
    if token in PATS:
        return "pat:" + token
    if token.startswith(OAUTH_TOKEN_PREFIX):
        return "oauth"
    if token.count(".") == 2 and jwt_claims(token) is not None:
        return "jwt"
    return "bearer:unknown"


def pat_user(req):
    """User of a known fixed PAT sent as Bearer, else None. IdP JWTs and foreign tokens never match."""
    token = req.bearer()
    login = PATS.get(token) if token else None
    return USERS[login] if login else None
