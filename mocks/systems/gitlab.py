"""GitLab mock: OAuth 2.0 provider (authorize, token, refresh, revoke), whoami and the REST API.

The broker (ws-06) runs "Polacz przez OAuth" against this: authorization code + PKCE S256 + state. The
authorize endpoint is opened by the browser, so MOCK_GITLAB_PUBLIC_URL (http://localhost:18741) is the
address the broker puts into authorizeUrl; the token endpoint is called by the broker from the compose
network (http://gitlab.internal:8080/oauth/token).

Query parameters of /oauth/authorize that only this mock understands (a real GitLab ignores them):
  mock_user=<login>       approve without the consent page as that user (alice, bob, carol, mallory)
  mock_action=deny        the user refuses: error=access_denied
  mock_scenario=<name>    wrong_state     the redirect carries a state different from the request
                          provider_error  error=server_error instead of a code
                          token_error     the token endpoint answers 500 for this code
                          refresh_revoked tokens of this grant cannot be refreshed (invalid_grant)
                          short_ttl       access token lives 30 s
State is in memory: a restart of the mock invalidates every code and token.
"""
import base64
import hashlib
import hmac
import html
import os
import secrets
import threading
import time
from urllib.parse import urlencode

import common
import gitlab_rest
from common import USERS, PATS, json_response, html_response, Response

CLIENT_ID = os.environ.get("MOCK_GITLAB_CLIENT_ID", "mcp-broker")
CLIENT_SECRET = os.environ.get("MOCK_GITLAB_CLIENT_SECRET", "")
REDIRECT_URIS = [u for u in os.environ.get("MOCK_GITLAB_REDIRECT_URIS", "").split(",") if u]
TOKEN_TTL = int(os.environ.get("MOCK_GITLAB_TOKEN_TTL", "7200"))
SHORT_TTL = 30
CODE_TTL = 120
LOGINS = ("alice", "bob", "carol", "mallory")
USER_IDS = {"alice": 101, "bob": 102, "carol": 103, "mallory": 104, "someone-else": 199}

_lock = threading.Lock()
_codes = {}
_access = {}
_refresh = {}


def _now():
    return time.time()


def gitlab_user(user):
    login = user["login"]
    return {"id": USER_IDS[login], "username": login, "name": user["name"], "state": "active",
            "email": user["email"], "web_url": f"http://gitlab.internal:8080/{login}",
            "avatar_url": None, "is_admin": login == "carol"}


def _oauth_error(status, error, description):
    return json_response(status, {"error": error, "error_description": description},
                         {"Cache-Control": "no-store", "Pragma": "no-cache"})


def _redirect(base, params):
    sep = "&" if "?" in base else "?"
    return Response(302, b"", {"Location": base + sep + urlencode(params), "Cache-Control": "no-store"},
                    content_type=None)


def _consent_page(req):
    query = {k: v[0] for k, v in req.query.items()}
    rows = []
    for login in LOGINS:
        link = "/oauth/authorize?" + urlencode({**query, "mock_user": login})
        rows.append(f'<li><a href="{html.escape(link)}">Authorize as {login}</a></li>')
    deny = "/oauth/authorize?" + urlencode({**query, "mock_action": "deny"})
    page = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<title>GitLab mock: authorize</title></head><body>"
            f"<h1>Authorize {html.escape(CLIENT_ID)}</h1>"
            "<p>Mock GitLab of mcp-platform-dev. Pick the GitLab account that grants access "
            f"(scope: {html.escape(query.get('scope', ''))}).</p><ul>" + "".join(rows) + "</ul>"
            f'<p><a href="{html.escape(deny)}">Deny</a></p></body></html>')
    return html_response(200, page, {"Cache-Control": "no-store"})


def _authorize(req):
    client_id = req.param("client_id", "")
    redirect_uri = req.param("redirect_uri", "")
    # A bad client or redirect_uri must never redirect (RFC 6749 4.1.2.1): the browser stays on an error page.
    if client_id != CLIENT_ID:
        return html_response(400, "<h1>Unknown client</h1>")
    if redirect_uri not in REDIRECT_URIS:
        return html_response(400, "<h1>redirect_uri is not registered</h1>")
    state = req.param("state")
    out = {"state": state} if state else {}
    if req.param("response_type") != "code":
        return _redirect(redirect_uri, {**out, "error": "unsupported_response_type"})
    challenge = req.param("code_challenge", "")
    if not challenge or req.param("code_challenge_method") != "S256":
        return _redirect(redirect_uri, {**out, "error": "invalid_request",
                                        "error_description": "PKCE with S256 is required"})
    if not state:
        return _redirect(redirect_uri, {"error": "invalid_request", "error_description": "state is required"})
    scenario = req.param("mock_scenario", "")
    if req.param("mock_action") == "deny":
        return _redirect(redirect_uri, {**out, "error": "access_denied",
                                        "error_description": "The resource owner or authorization server denied the request."})
    if scenario == "provider_error":
        return _redirect(redirect_uri, {**out, "error": "server_error"})
    login = req.param("mock_user")
    if login is None:
        return _consent_page(req)
    if login not in LOGINS:
        return html_response(400, "<h1>Unknown mock_user</h1>")
    code = "glc-" + secrets.token_urlsafe(24)
    with _lock:
        _codes[code] = {"login": login, "challenge": challenge, "redirect_uri": redirect_uri,
                        "scope": req.param("scope", ""), "scenario": scenario, "expires": _now() + CODE_TTL,
                        "used": False}
    returned_state = state + "-tampered" if scenario == "wrong_state" else state
    return _redirect(redirect_uri, {"code": code, "state": returned_state})


def _client_ok(req, form):
    client_id, secret = form.get("client_id"), form.get("client_secret")
    creds = req.basic()
    if creds is not None:
        client_id, secret = creds
    if client_id != CLIENT_ID or not CLIENT_SECRET or secret is None:
        return False
    return hmac.compare_digest(secret.encode(), CLIENT_SECRET.encode())


def _issue(login, scope, scenario):
    ttl = SHORT_TTL if scenario == "short_ttl" else TOKEN_TTL
    access, refresh = "glo-" + secrets.token_urlsafe(24), "glr-" + secrets.token_urlsafe(24)
    now = _now()
    _access[access] = {"login": login, "expires": now + ttl, "scenario": scenario, "scope": scope, "refresh": refresh}
    _refresh[refresh] = {"login": login, "scope": scope, "scenario": scenario, "access": access}
    return json_response(200, {"access_token": access, "token_type": "Bearer", "expires_in": ttl,
                               "refresh_token": refresh, "scope": scope or "api", "created_at": int(now)},
                         {"Cache-Control": "no-store", "Pragma": "no-cache"})


def _token(req):
    if req.method != "POST":
        return json_response(405, {"error": "invalid_request"})
    form = req.form()
    if not _client_ok(req, form):
        return _oauth_error(401, "invalid_client", "Client authentication failed due to unknown client, "
                            "no client authentication included, or unsupported authentication method.")
    grant = form.get("grant_type")
    with _lock:
        if grant == "authorization_code":
            return _exchange_code(form)
        if grant == "refresh_token":
            return _refresh_grant(form)
    return _oauth_error(400, "unsupported_grant_type", "The authorization grant type is not supported.")


def _exchange_code(form):
    record = _codes.get(form.get("code", ""))
    if record is None or record["used"] or record["expires"] < _now():
        return _oauth_error(400, "invalid_grant", "The provided authorization grant is invalid, expired, "
                            "revoked, does not match the redirection URI, or was issued to another client.")
    record["used"] = True
    if form.get("redirect_uri") != record["redirect_uri"]:
        return _oauth_error(400, "invalid_grant", "redirect_uri does not match the authorization request.")
    verifier = form.get("code_verifier", "")
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    if not verifier or not hmac.compare_digest(digest, record["challenge"]):
        return _oauth_error(400, "invalid_grant", "code_verifier does not match code_challenge.")
    if record["scenario"] == "token_error":
        return _oauth_error(500, "server_error", "Mock provider failure requested by mock_scenario=token_error.")
    return _issue(record["login"], record["scope"], record["scenario"])


def _refresh_grant(form):
    token = form.get("refresh_token", "")
    record = _refresh.pop(token, None)
    if record is None or record["scenario"] == "refresh_revoked":
        return _oauth_error(400, "invalid_grant", "The provided authorization grant is invalid, expired, "
                            "revoked, does not match the redirection URI, or was issued to another client.")
    _access.pop(record["access"], None)
    return _issue(record["login"], record["scope"], record["scenario"])


def _revoke(req):
    form = req.form()
    if not _client_ok(req, form):
        return _oauth_error(401, "invalid_client", "Client authentication failed.")
    token = form.get("token", "")
    with _lock:
        record = _access.pop(token, None)
        if record is not None:
            _refresh.pop(record["refresh"], None)
        elif token in _refresh:
            _access.pop(_refresh.pop(token)["access"], None)
    return json_response(200, {})


def _unauthorized(description=None):
    if description:
        return json_response(401, {"error": "invalid_token", "error_description": description},
                             {"WWW-Authenticate": 'Bearer realm="Protected by OAuth 2.0", error="invalid_token"'})
    return json_response(401, {"message": "401 Unauthorized"})


def _authenticate(req):
    """(user, None) or (None, error response). PAT as Bearer or PRIVATE-TOKEN, OAuth access token as Bearer."""
    token = req.bearer() or req.header("private-token")
    if not token:
        return None, _unauthorized()
    login = PATS.get(token)
    if login:
        return USERS[login], None
    if token.startswith(common.OAUTH_TOKEN_PREFIX) and req.bearer():
        with _lock:
            record = _access.get(token)
        if record is None:
            return None, _unauthorized("The access token is invalid, revoked or was replaced by a refresh.")
        if record["expires"] < _now():
            return None, _unauthorized("The access token expired")
        return USERS[record["login"]], None
    return None, _unauthorized()


def handle(req):
    path = req.raw_path
    if path == "/oauth/authorize" and req.method == "GET":
        return _authorize(req)
    if path == "/oauth/token":
        return _token(req)
    if path == "/oauth/revoke" and req.method == "POST":
        return _revoke(req)
    if req.segments[:2] == ["api", "v4"]:
        user, error = _authenticate(req)
        if error is not None:
            return error
        if req.segments == ["api", "v4", "user"] and req.method == "GET":
            return json_response(200, gitlab_user(user))
        return gitlab_rest.handle_rest(req, user)
    return None
