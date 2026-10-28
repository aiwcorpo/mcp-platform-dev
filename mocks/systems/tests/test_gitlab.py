import base64
import hashlib
import json
import os
import sys
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REDIRECT = "http://localhost:18740/api/v1/oauth/callback"
os.environ["MOCK_GITLAB_CLIENT_SECRET"] = "client-secret-for-tests"
os.environ["MOCK_GITLAB_REDIRECT_URIS"] = REDIRECT

import common  # noqa: E402
import gitlab  # noqa: E402

VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


def get(path, query=None, headers=None):
    target = path + ("?" + urlencode(query) if query else "")
    return gitlab.handle(common.Request("GET", target, headers or {}, b""))


def post_form(path, form, headers=None):
    return gitlab.handle(common.Request("POST", path, headers or {}, urlencode(form).encode()))


def authorize_query(**extra):
    query = {"client_id": "mcp-broker", "response_type": "code", "redirect_uri": REDIRECT, "state": "st-1",
             "code_challenge": CHALLENGE, "code_challenge_method": "S256", "scope": "read_api"}
    query.update(extra)
    return query


def redirect_params(resp):
    assert resp.status == 302, resp.status
    location = urlsplit(resp.headers["Location"])
    assert resp.headers["Location"].startswith(REDIRECT)
    return {k: v[0] for k, v in parse_qs(location.query).items()}


def token_form(code, **extra):
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": "mcp-broker", "client_secret": "client-secret-for-tests", "code_verifier": VERIFIER}
    form.update(extra)
    return form


def body(resp):
    return json.loads(resp.body)


def obtain_tokens(**authorize_extra):
    params = redirect_params(get("/oauth/authorize", authorize_query(mock_user="alice", **authorize_extra)))
    resp = post_form("/oauth/token", token_form(params["code"]))
    assert resp.status == 200, resp.body
    return body(resp)


class AuthorizeTest(unittest.TestCase):
    def test_without_user_shows_consent_page_with_accounts_and_deny(self):
        resp = get("/oauth/authorize", authorize_query())
        self.assertEqual(200, resp.status)
        page = resp.body.decode()
        for login in ("alice", "bob", "carol", "mallory"):
            self.assertIn("mock_user=" + login, page)
        self.assertIn("mock_action=deny", page)

    def test_mock_user_redirects_with_code_and_the_same_state(self):
        params = redirect_params(get("/oauth/authorize", authorize_query(mock_user="alice")))
        self.assertEqual("st-1", params["state"])
        self.assertTrue(params["code"].startswith("glc-"))

    def test_deny_gives_access_denied_with_state(self):
        params = redirect_params(get("/oauth/authorize", authorize_query(mock_action="deny")))
        self.assertEqual({"error", "error_description", "state"}, set(params))
        self.assertEqual("access_denied", params["error"])
        self.assertEqual("st-1", params["state"])

    def test_provider_error_scenario(self):
        params = redirect_params(get("/oauth/authorize", authorize_query(mock_user="alice", mock_scenario="provider_error")))
        self.assertEqual("server_error", params["error"])
        self.assertNotIn("code", params)

    def test_wrong_state_scenario_returns_a_different_state(self):
        params = redirect_params(get("/oauth/authorize", authorize_query(mock_user="alice", mock_scenario="wrong_state")))
        self.assertIn("code", params)
        self.assertNotEqual("st-1", params["state"])

    def test_unknown_client_or_redirect_uri_never_redirects(self):
        self.assertEqual(400, get("/oauth/authorize", authorize_query(client_id="other", mock_user="alice")).status)
        resp = get("/oauth/authorize", authorize_query(redirect_uri="http://localhost:9/cb", mock_user="alice"))
        self.assertEqual(400, resp.status)
        self.assertNotIn("Location", resp.headers)

    def test_pkce_s256_and_state_are_required(self):
        no_pkce = authorize_query(mock_user="alice")
        del no_pkce["code_challenge"]
        self.assertEqual("invalid_request", redirect_params(get("/oauth/authorize", no_pkce))["error"])
        plain = authorize_query(mock_user="alice", code_challenge_method="plain")
        self.assertEqual("invalid_request", redirect_params(get("/oauth/authorize", plain))["error"])
        no_state = authorize_query(mock_user="alice")
        del no_state["state"]
        self.assertEqual("invalid_request", redirect_params(get("/oauth/authorize", no_state))["error"])

    def test_unknown_mock_user_is_refused(self):
        self.assertEqual(400, get("/oauth/authorize", authorize_query(mock_user="nobody")).status)


class TokenTest(unittest.TestCase):
    def code(self, **extra):
        return redirect_params(get("/oauth/authorize", authorize_query(mock_user="alice", **extra)))["code"]

    def test_code_with_verifier_gives_tokens(self):
        resp = post_form("/oauth/token", token_form(self.code()))
        self.assertEqual(200, resp.status)
        tokens = body(resp)
        self.assertEqual("Bearer", tokens["token_type"])
        self.assertTrue(tokens["access_token"].startswith("glo-"))
        self.assertTrue(tokens["refresh_token"].startswith("glr-"))
        self.assertEqual("no-store", resp.headers["Cache-Control"])

    def test_client_credentials_in_basic_auth_are_accepted(self):
        basic = "Basic " + base64.b64encode(b"mcp-broker:client-secret-for-tests").decode()
        form = token_form(self.code())
        del form["client_id"], form["client_secret"]
        self.assertEqual(200, post_form("/oauth/token", form, {"Authorization": basic}).status)

    def test_wrong_verifier_is_invalid_grant(self):
        resp = post_form("/oauth/token", token_form(self.code(), code_verifier="w" * 64))
        self.assertEqual(400, resp.status)
        self.assertEqual("invalid_grant", body(resp)["error"])

    def test_missing_verifier_is_invalid_grant(self):
        resp = post_form("/oauth/token", token_form(self.code(), code_verifier=""))
        self.assertEqual("invalid_grant", body(resp)["error"])

    def test_code_is_single_use(self):
        code = self.code()
        self.assertEqual(200, post_form("/oauth/token", token_form(code)).status)
        self.assertEqual(400, post_form("/oauth/token", token_form(code)).status)

    def test_wrong_redirect_uri_is_invalid_grant(self):
        resp = post_form("/oauth/token", token_form(self.code(), redirect_uri="http://localhost:18090/api/v1/oauth/callback"))
        self.assertEqual("invalid_grant", body(resp)["error"])

    def test_wrong_client_secret_is_invalid_client(self):
        resp = post_form("/oauth/token", token_form(self.code(), client_secret="nope"))
        self.assertEqual(401, resp.status)
        self.assertEqual("invalid_client", body(resp)["error"])

    def test_unknown_code_and_grant_type(self):
        self.assertEqual("invalid_grant", body(post_form("/oauth/token", token_form("glc-unknown")))["error"])
        resp = post_form("/oauth/token", {**token_form("x"), "grant_type": "password"})
        self.assertEqual("unsupported_grant_type", body(resp)["error"])

    def test_token_error_scenario_answers_500(self):
        resp = post_form("/oauth/token", token_form(self.code(mock_scenario="token_error")))
        self.assertEqual(500, resp.status)

    def test_get_on_token_endpoint_is_refused(self):
        self.assertEqual(405, get("/oauth/token").status)


class RefreshTest(unittest.TestCase):
    def refresh(self, token):
        return post_form("/oauth/token", {"grant_type": "refresh_token", "refresh_token": token,
                                           "client_id": "mcp-broker", "client_secret": "client-secret-for-tests"})

    def test_refresh_rotates_both_tokens_and_invalidates_the_old_ones(self):
        first = obtain_tokens()
        resp = self.refresh(first["refresh_token"])
        self.assertEqual(200, resp.status)
        second = body(resp)
        self.assertNotEqual(first["access_token"], second["access_token"])
        self.assertEqual(401, get("/api/v4/user", headers={"Authorization": "Bearer " + first["access_token"]}).status)
        self.assertEqual(200, get("/api/v4/user", headers={"Authorization": "Bearer " + second["access_token"]}).status)
        self.assertEqual("invalid_grant", body(self.refresh(first["refresh_token"]))["error"])

    def test_refresh_revoked_scenario(self):
        tokens = obtain_tokens(mock_scenario="refresh_revoked")
        self.assertEqual("invalid_grant", body(self.refresh(tokens["refresh_token"]))["error"])

    def test_short_ttl_scenario_and_unknown_refresh_token(self):
        self.assertEqual(30, obtain_tokens(mock_scenario="short_ttl")["expires_in"])
        self.assertEqual(400, self.refresh("glr-unknown").status)

    def test_expired_access_token_is_rejected(self):
        tokens = obtain_tokens()
        gitlab._access[tokens["access_token"]]["expires"] = 0
        resp = get("/api/v4/user", headers={"Authorization": "Bearer " + tokens["access_token"]})
        self.assertEqual(401, resp.status)
        self.assertIn("expired", body(resp)["error_description"])


class WhoamiTest(unittest.TestCase):
    def test_oauth_token_identifies_the_authorizing_user(self):
        params = redirect_params(get("/oauth/authorize", authorize_query(mock_user="bob")))
        tokens = body(post_form("/oauth/token", token_form(params["code"])))
        resp = get("/api/v4/user", headers={"Authorization": "Bearer " + tokens["access_token"]})
        self.assertEqual("bob", body(resp)["username"])

    def test_known_pats_as_bearer_and_private_token(self):
        for pat, login in (("alice-pat", "alice"), ("bob-pat", "bob"), ("mallory-pat", "mallory")):
            self.assertEqual(login, body(get("/api/v4/user", headers={"Authorization": "Bearer " + pat}))["username"])
            self.assertEqual(login, body(get("/api/v4/user", headers={"PRIVATE-TOKEN": pat}))["username"])

    def test_unknown_tokens_and_idp_jwt_are_401(self):
        jwt = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJhbGljZSJ9.sig"
        for headers in ({}, {"Authorization": "Bearer other-pat"}, {"Authorization": "Bearer " + jwt},
                        {"PRIVATE-TOKEN": "other-pat"}, {"Authorization": "Bearer glo-unknown"},
                        {"Authorization": "Basic YWxpY2U6eA=="}):
            self.assertEqual(401, get("/api/v4/user", headers=headers).status, headers)

    def test_oauth_token_in_private_token_header_is_not_accepted(self):
        tokens = obtain_tokens()
        self.assertEqual(401, get("/api/v4/user", headers={"PRIVATE-TOKEN": tokens["access_token"]}).status)

    def test_revoked_token_stops_working(self):
        tokens = obtain_tokens()
        resp = post_form("/oauth/revoke", {"token": tokens["access_token"], "client_id": "mcp-broker",
                                           "client_secret": "client-secret-for-tests"})
        self.assertEqual(200, resp.status)
        self.assertEqual(401, get("/api/v4/user", headers={"Authorization": "Bearer " + tokens["access_token"]}).status)

    def test_rest_api_needs_the_same_authentication(self):
        self.assertEqual(401, get("/api/v4/projects/42").status)
        self.assertEqual(200, get("/api/v4/projects/42", headers={"Authorization": "Bearer alice-pat"}).status)

    def test_unknown_path_is_none(self):
        self.assertIsNone(get("/nothing"))


if __name__ == "__main__":
    unittest.main()
