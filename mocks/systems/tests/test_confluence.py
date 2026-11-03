import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common  # noqa: E402
import confluence  # noqa: E402

RESERVED_CQL = 'space = "ENG" AND type = page AND text ~ "%s"'


def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


JWT = ".".join([b64({"alg": "none"}), b64({"iss": "http://idp", "preferred_username": "alice"}), "sig"])


def call(method, target, token="alice-pat", body=None, scheme="Bearer"):
    headers = {"Authorization": "%s %s" % (scheme, token)} if token is not None else {}
    data = json.dumps(body).encode() if body is not None else b""
    resp = confluence.handle(common.Request(method, target, headers, data))
    return resp.status, json.loads(resp.body) if resp.body else None


def search(cql, token="alice-pat", extra=""):
    from urllib.parse import quote
    return call("GET", "/rest/api/content/search?cql=%s&limit=25&expand=space,version%s" % (quote(cql), extra), token)


def new_page(**override):
    payload = {"type": "page", "title": "Fresh page", "space": {"key": "ENG"}, "ancestors": [{"id": "10001"}],
               "body": {"storage": {"value": "<p>hi</p>", "representation": "storage"}}}
    payload.update(override)
    return payload


class WhoamiTest(unittest.TestCase):
    def test_known_pats(self):
        for pat, login in (("alice-pat", "alice"), ("bob-pat", "bob"), ("carol-pat", "carol"),
                           ("mallory-pat", "mallory")):
            status, body = call("GET", "/rest/api/user/current", pat)
            self.assertEqual((status, body["username"]), (200, login))

    def test_foreign_token_is_someone_else(self):
        status, body = call("GET", "/rest/api/user/current", "test-only-confluence-pat-0000")
        self.assertEqual((status, body["username"]), (200, "someone-else"))

    def test_missing_credentials(self):
        status, body = call("GET", "/rest/api/user/current", None)
        self.assertEqual((status, body["statusCode"]), (401, 401))

    def test_jwt_and_basic_are_not_identities(self):
        self.assertEqual(call("GET", "/rest/api/user/current", JWT)[0], 401)
        self.assertEqual(call("GET", "/rest/api/user/current", "alice:x", scheme="Basic")[0], 401)


class AuthOnDataTest(unittest.TestCase):
    TARGETS = [("GET", "/rest/api/content/10001?expand=space"), ("GET", "/rest/api/content/search?cql=space%3DENG"),
               ("POST", "/rest/api/content")]

    def test_no_data_without_a_known_pat(self):
        for token, scheme in ((None, "Bearer"), (JWT, "Bearer"), ("someone-elses-pat", "Bearer"),
                              ("alice-pat", "Basic")):
            for method, target in self.TARGETS:
                status, body = call(method, target, token, new_page() if method == "POST" else None, scheme)
                self.assertEqual(status, 401, (token, method, target))
                self.assertEqual(body["statusCode"], 401)

    def test_every_known_pat_sees_data(self):
        for pat in common.PATS:
            self.assertEqual(call("GET", "/rest/api/content/10001", pat)[0], 200)

    def test_unrelated_path_is_not_handled(self):
        self.assertIsNone(confluence.handle(common.Request("GET", "/other", {}, b"")))


class GetPageTest(unittest.TestCase):
    def test_page_with_ancestors_space_version_and_storage(self):
        status, page = call("GET", "/rest/api/content/10003?expand=body.storage,space,version,ancestors")
        self.assertEqual(status, 200)
        self.assertEqual((page["id"], page["type"], page["status"]), ("10003", "page", "current"))
        self.assertEqual(page["space"]["key"], "ENG")
        self.assertEqual(page["version"]["number"], 7)
        self.assertTrue(page["version"]["when"].startswith("2026-"))
        self.assertEqual([a["id"] for a in page["ancestors"]], ["10001", "10002"])
        self.assertIn('ac:name="code"', page["body"]["storage"]["value"])

    def test_expand_limits_the_fields(self):
        _, page = call("GET", "/rest/api/content/10003?expand=space,version")
        self.assertNotIn("body", page)
        self.assertNotIn("ancestors", page)
        self.assertIn("version", page)

    def test_macro_pages_carry_includes_in_storage(self):
        _, page = call("GET", "/rest/api/content/10004?expand=body.storage")
        value = page["body"]["storage"]["value"]
        for name in ("include", "excerpt-include", "contentbylabel", "note"):
            self.assertIn('ac:name="%s"' % name, value)

    def test_injection_page_is_plain_content(self):
        _, page = call("GET", "/rest/api/content/10005?expand=body.storage")
        self.assertIn("ignore all previous instructions", page["body"]["storage"]["value"])

    def test_blog_and_personal_space_are_returned_as_they_are(self):
        self.assertEqual(call("GET", "/rest/api/content/10007")[1]["type"], "blogpost")
        _, personal = call("GET", "/rest/api/content/40001?expand=space")
        self.assertEqual(personal["space"]["key"], "~alice")

    def test_unknown_id_is_confluence_404(self):
        status, body = call("GET", "/rest/api/content/99999999")
        self.assertEqual(status, 404)
        self.assertEqual(body["statusCode"], 404)
        self.assertIn("No content found with id", body["message"])

    def test_non_numeric_id_is_404(self):
        self.assertEqual(call("GET", "/rest/api/content/abc")[0], 404)

    def test_hr_page_is_served_without_permission_model(self):
        self.assertEqual(call("GET", "/rest/api/content/20002", "mallory-pat")[0], 200)


class SearchTest(unittest.TestCase):
    def test_space_type_and_text(self):
        status, body = search(RESERVED_CQL % "deploy runbook")
        self.assertEqual(status, 200)
        ids = [r["id"] for r in body["results"]]
        self.assertEqual(ids, ["10003"])
        self.assertEqual(body["results"][0]["space"]["key"], "ENG")
        self.assertEqual(body["results"][0]["version"]["number"], 7)

    def test_type_page_excludes_blog(self):
        _, body = search(RESERVED_CQL % "deploy")
        ids = [r["id"] for r in body["results"]]
        self.assertIn("10003", ids)
        self.assertNotIn("10007", ids)

    def test_blog_found_when_asked(self):
        _, body = search('space = "ENG" AND type = blogpost AND text ~ "update"')
        self.assertEqual([r["id"] for r in body["results"]], ["10007"])

    def test_other_space_does_not_leak(self):
        _, body = search('space = "OPS" AND type = page AND text ~ "salary"')
        self.assertEqual(body["results"], [])
        _, body = search('space = "HR" AND type = page AND text ~ "salary"')
        self.assertEqual([r["id"] for r in body["results"]], ["20002"])

    def test_case_insensitive_and_limit(self):
        _, body = search(RESERVED_CQL % "RUNBOOK")
        self.assertGreaterEqual(len(body["results"]), 2)
        _, one = call("GET", "/rest/api/content/search?cql=space%3D%22ENG%22&limit=1")
        self.assertEqual(len(one["results"]), 1)

    def test_no_match_is_empty_list(self):
        status, body = search(RESERVED_CQL % "nonexistentword")
        self.assertEqual((status, body["results"]), (200, []))

    def test_unknown_space_is_400(self):
        status, body = search('space = "NOPE" AND type = page AND text ~ "x"')
        self.assertEqual((status, body["statusCode"]), (400, 400))

    def test_unparsable_or_missing_cql_is_400(self):
        self.assertEqual(search("this is not cql")[0], 400)
        self.assertEqual(call("GET", "/rest/api/content/search")[0], 400)
        self.assertEqual(search('space = "ENG" AND text ~ ""')[0], 400)

    def test_search_is_not_a_page_id(self):
        status, _ = call("GET", "/rest/api/content/search?cql=space%3DENG")
        self.assertEqual(status, 200)


class CreatePageTest(unittest.TestCase):
    def test_created_page_is_echoed_but_not_stored(self):
        status, page = call("POST", "/rest/api/content", body=new_page(ancestors=[{"id": "10002"}]))
        self.assertEqual(status, 200)
        self.assertEqual((page["id"], page["title"], page["space"]["key"]), ("90001", "Fresh page", "ENG"))
        self.assertEqual(page["version"]["number"], 1)
        self.assertEqual([a["id"] for a in page["ancestors"]], ["10001", "10002"])
        self.assertEqual(call("GET", "/rest/api/content/90001")[0], 404)

    def test_duplicate_title_is_400(self):
        status, body = call("POST", "/rest/api/content", body=new_page(title="deploy runbook"))
        self.assertEqual(status, 400)
        self.assertIn("already exists", body["message"])

    def test_parent_from_another_space_is_400(self):
        self.assertEqual(call("POST", "/rest/api/content", body=new_page(ancestors=[{"id": "20001"}]))[0], 400)

    def test_unknown_parent_unknown_space_and_bad_body_are_400(self):
        self.assertEqual(call("POST", "/rest/api/content", body=new_page(ancestors=[{"id": "1"}]))[0], 400)
        self.assertEqual(call("POST", "/rest/api/content", body=new_page(space={"key": "NOPE"}))[0], 400)
        self.assertEqual(call("POST", "/rest/api/content", body=new_page(title="  "))[0], 400)
        self.assertEqual(call("POST", "/rest/api/content", body=new_page(body={"storage": {"value": "x"}}))[0], 400)
        status, _ = confluence_post_raw(b"not json")
        self.assertEqual(status, 400)


def confluence_post_raw(raw):
    resp = confluence.handle(common.Request("POST", "/rest/api/content",
                                            {"Authorization": "Bearer alice-pat"}, raw))
    return resp.status, json.loads(resp.body)


class RoutingTest(unittest.TestCase):
    def test_unknown_rest_path_is_confluence_404(self):
        status, body = call("GET", "/rest/api/space")
        self.assertEqual((status, body["statusCode"]), (404, 404))

    def test_wrong_method_is_not_served(self):
        self.assertEqual(call("DELETE", "/rest/api/content/10001")[0], 404)
        self.assertEqual(call("POST", "/rest/api/user/current")[0], 405)


class SpaceLabels(unittest.TestCase):
    def test_labels_are_complete_and_hr_is_restricted(self):
        status, body = call("GET", "/rest/api/space/HR?expand=metadata.labels")
        labels = body["metadata"]["labels"]
        self.assertEqual(status, 200)
        self.assertEqual([x["name"] for x in labels["results"]], ["restricted"])
        self.assertEqual((labels["size"], labels["limit"]), (1, 200))

    def test_personal_space_key_may_be_encoded_and_unknown_is_404(self):
        self.assertEqual(call("GET", "/rest/api/space/%7Ealice?expand=metadata.labels")[0], 200)
        self.assertEqual(call("GET", "/rest/api/space/NOPE")[0], 404)

    def test_needs_a_known_pat(self):
        self.assertEqual(call("GET", "/rest/api/space/ENG", token="unknown")[0], 401)


class VersionProbeTest(unittest.TestCase):
    def test_server_information_is_anonymous_and_in_the_supported_range(self):
        status, body = call("GET", "/rest/api/server-information", token=None)
        self.assertEqual(200, status)
        self.assertEqual("9.2.1", body["version"])


if __name__ == "__main__":
    unittest.main()
