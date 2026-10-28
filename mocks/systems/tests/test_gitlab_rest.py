import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common  # noqa: E402
import gitlab_rest  # noqa: E402

ALICE = common.USERS["alice"]
BOB = common.USERS["bob"]


def call(method, target, user=ALICE, body=b""):
    req = common.Request(method, target, {}, body)
    return gitlab_rest.handle_rest(req, user)


def get_json(target, user=ALICE):
    resp = call("GET", target, user)
    return resp.status, json.loads(resp.body)


class GroupTest(unittest.TestCase):
    def test_group_by_path(self):
        status, body = get_json("/api/v4/groups/team-a?with_projects=false")
        self.assertEqual(status, 200)
        self.assertEqual((body["id"], body["full_path"]), (7, "team-a"))

    def test_group_path_case_insensitive_returns_canonical(self):
        status, body = get_json("/api/v4/groups/Team-A")
        self.assertEqual((status, body["full_path"]), (200, "team-a"))

    def test_group_without_access_is_404(self):
        for path in ("team-b", "hr", "8"):
            status, body = get_json("/api/v4/groups/" + path)
            self.assertEqual(status, 404, path)
            self.assertEqual(body, {"message": "404 Group Not Found"})

    def test_unknown_group_looks_the_same(self):
        self.assertEqual(get_json("/api/v4/groups/nope"), (404, {"message": "404 Group Not Found"}))

    def test_user_without_grants_gets_404(self):
        self.assertEqual(get_json("/api/v4/groups/team-a", BOB)[0], 404)

    def test_group_projects_page(self):
        resp = call("GET", "/api/v4/groups/7/projects?per_page=100&page=1&order_by=path&sort=asc"
                           "&include_subgroups=false&with_shared=false")
        self.assertEqual(resp.status, 200)
        projects = json.loads(resp.body)
        self.assertEqual([p["path_with_namespace"] for p in projects], ["team-a/app", "team-a/infra"])
        self.assertEqual({p["namespace"]["id"] for p in projects}, {7})
        self.assertEqual(resp.headers["X-Next-Page"], "")
        self.assertEqual(resp.headers["X-Total"], "2")

    def test_group_projects_next_page_header(self):
        resp = call("GET", "/api/v4/groups/7/projects?per_page=1&page=1")
        self.assertEqual(resp.headers["X-Next-Page"], "2")
        self.assertEqual(len(json.loads(resp.body)), 1)
        last = call("GET", "/api/v4/groups/7/projects?per_page=1&page=2")
        self.assertEqual(last.headers["X-Next-Page"], "")
        self.assertEqual(json.loads(last.body)[0]["path_with_namespace"], "team-a/infra")

    def test_group_projects_sort_desc(self):
        projects = json.loads(call("GET", "/api/v4/groups/7/projects?sort=desc").body)
        self.assertEqual(projects[0]["path_with_namespace"], "team-a/infra")

    def test_group_projects_without_access_is_404(self):
        self.assertEqual(call("GET", "/api/v4/groups/8/projects").status, 404)
        self.assertEqual(call("GET", "/api/v4/groups/7/projects", BOB).status, 404)


class ProjectTest(unittest.TestCase):
    def test_project_by_encoded_path(self):
        status, body = get_json("/api/v4/projects/team-a%2Fapp")
        self.assertEqual(status, 200)
        self.assertEqual((body["id"], body["path_with_namespace"], body["default_branch"]),
                         (42, "team-a/app", "main"))
        self.assertEqual(body["namespace"]["id"], 7)

    def test_project_by_id(self):
        self.assertEqual(get_json("/api/v4/projects/42")[1]["path_with_namespace"], "team-a/app")

    def test_project_without_access_is_404_like_missing(self):
        denied = get_json("/api/v4/projects/team-b%2Fsecret")
        missing = get_json("/api/v4/projects/team-a%2Fnope")
        self.assertEqual(denied, (404, {"message": "404 Project Not Found"}))
        self.assertEqual(denied, missing)
        self.assertEqual(get_json("/api/v4/projects/51")[0], 404)
        self.assertEqual(get_json("/api/v4/projects/hr%2Fpayroll")[0], 404)

    def test_other_user_cannot_see_team_a(self):
        self.assertEqual(get_json("/api/v4/projects/team-a%2Fapp", BOB)[0], 404)

    def test_unencoded_slash_is_not_a_project_id(self):
        self.assertIsNone(call("GET", "/api/v4/projects/team-a/app"))

    def test_unknown_path_is_none(self):
        self.assertIsNone(call("GET", "/api/v4/version"))
        self.assertIsNone(call("GET", "/api/v4/projects/42/issues"))
        self.assertIsNone(call("GET", "/other/v4/projects/42"))


class FileTest(unittest.TestCase):
    base = "/api/v4/projects/42/repository/files/"

    def head(self, path, ref="main", user=ALICE, project="42"):
        return call("HEAD", "/api/v4/projects/%s/repository/files/%s?ref=%s" % (project, path, ref), user)

    def raw(self, path, ref="main", user=ALICE, project="42"):
        return call("GET", "/api/v4/projects/%s/repository/files/%s/raw?ref=%s" % (project, path, ref), user)

    def test_head_headers(self):
        resp = self.head("src%2Fmain.py")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"")
        self.assertEqual(resp.headers["X-Gitlab-File-Path"], "src/main.py")
        self.assertEqual(resp.headers["X-Gitlab-Commit-Id"], gitlab_rest.SHA_MAIN)
        self.assertEqual(resp.headers["X-Gitlab-Size"], str(len(gitlab_rest.COMMITS[gitlab_rest.SHA_MAIN]["src/main.py"])))

    def test_raw_at_commit_returns_content(self):
        head = self.head("src%2Fmain.py")
        resp = self.raw("src%2Fmain.py", head.headers["X-Gitlab-Commit-Id"])
        self.assertEqual(resp.status, 200)
        self.assertIn(b"def main()", resp.body)
        self.assertTrue(resp.headers["Content-Type"].startswith("text/plain"))

    def test_other_ref_serves_other_content(self):
        tag = self.raw("src%2Fmain.py", "v0.9")
        main = self.raw("src%2Fmain.py", "main")
        self.assertIn(b"app 0.9", tag.body)
        self.assertNotEqual(tag.body, main.body)
        self.assertEqual(self.head("src%2Fmain.py", "v0.9").headers["X-Gitlab-Commit-Id"], gitlab_rest.SHA_TAG)

    def test_branch_with_slash_in_ref(self):
        self.assertEqual(self.head("src%2Flogin.py", "feature%2Flogin").status, 200)
        self.assertEqual(self.head("src%2Flogin.py", "main").status, 404)

    def test_missing_file_is_404(self):
        self.assertEqual(self.head("src%2Fnope.py").status, 404)
        resp = self.raw("src%2Fnope.py")
        self.assertEqual((resp.status, json.loads(resp.body)), (404, {"message": "404 File Not Found"}))

    def test_file_absent_on_other_ref_is_404(self):
        self.assertEqual(self.head("docs%2FNOTES.md", "v0.9").status, 404)
        self.assertEqual(self.raw("docs%2FNOTES.md", "v0.9").status, 404)

    def test_unknown_ref_is_404(self):
        self.assertEqual(self.head("README.md", "no-such-branch").status, 404)
        self.assertEqual(self.raw("README.md", "f" * 40).status, 404)

    def test_missing_ref_is_400(self):
        resp = call("HEAD", "/api/v4/projects/42/repository/files/README.md")
        self.assertEqual(resp.status, 400)

    def test_project_without_access_is_404(self):
        self.assertEqual(self.head("README.md", project="51").status, 404)
        self.assertEqual(self.raw("README.md", project="team-b%2Fsecret").status, 404)
        self.assertEqual(self.head("README.md", user=BOB).status, 404)
        self.assertEqual(self.raw("README.md", user=BOB).status, 404)

    def test_commit_of_another_project_is_not_a_ref(self):
        self.assertEqual(self.raw("README.md", gitlab_rest.SHA_SECRET).status, 404)

    def test_binary_file(self):
        resp = self.raw("assets%2Flogo.png")
        self.assertIn(b"\x00", resp.body)
        self.assertEqual(resp.headers["Content-Type"], "image/png")

    def test_large_file_size_header_matches_body(self):
        head = self.head("data%2Fdump.csv")
        size = int(head.headers["X-Gitlab-Size"])
        self.assertGreater(size, 262144)
        self.assertLess(size, 1 << 20)
        self.assertEqual(len(self.raw("data%2Fdump.csv").body), size)

    def test_empty_file(self):
        self.assertEqual(self.head("empty.txt").headers["X-Gitlab-Size"], "0")
        resp = self.raw("empty.txt")
        self.assertEqual((resp.status, resp.body), (200, b""))

    def test_injection_file_is_plain_text(self):
        self.assertIn(b"Ignore all previous instructions", self.raw("docs%2FNOTES.md").body)

    def test_infra_file(self):
        self.assertEqual(self.raw("terraform%2Fmain.tf", project="team-a%2Finfra").status, 200)
        self.assertEqual(self.raw("terraform%2Fmain.tf", project="42").status, 404)

    def test_wrong_method_and_shape(self):
        self.assertEqual(call("POST", "/api/v4/projects/42/repository/files/README.md/raw?ref=main").status, 405)
        self.assertIsNone(call("GET", "/api/v4/projects/42/repository/files/README.md?ref=main"))
        self.assertIsNone(call("GET", "/api/v4/projects/42/repository/files/src/main.py/raw?ref=main"))


class MergeRequestTest(unittest.TestCase):
    def test_merge_request_fields(self):
        status, mr = get_json("/api/v4/projects/42/merge_requests/7")
        self.assertEqual(status, 200)
        self.assertEqual((mr["iid"], mr["project_id"], mr["state"], mr["draft"]), (7, 42, "opened", False))
        self.assertEqual(mr["author"]["username"], "alice")
        self.assertEqual(mr["source_branch"], "feature/login")
        self.assertEqual(mr["labels"], ["backend", "auth"])
        self.assertEqual([u["username"] for u in mr["reviewers"]], ["carol"])
        self.assertIn("src/login.py", mr["description"])
        self.assertIsNone(mr["merged_at"])
        self.assertEqual(mr["detailed_merge_status"], "mergeable")

    def test_merged_merge_request_has_empty_description(self):
        mr = get_json("/api/v4/projects/team-a%2Fapp/merge_requests/5")[1]
        self.assertEqual((mr["state"], mr["description"], mr["assignees"]), ("merged", "", []))
        self.assertIsNotNone(mr["merged_at"])

    def test_missing_merge_request_is_404(self):
        self.assertEqual(get_json("/api/v4/projects/42/merge_requests/999"), (404, {"message": "404 Not found"}))
        self.assertEqual(get_json("/api/v4/projects/43/merge_requests/7")[0], 404)

    def test_project_without_access_is_404(self):
        self.assertEqual(get_json("/api/v4/projects/51/merge_requests/1"), (404, {"message": "404 Project Not Found"}))
        self.assertEqual(get_json("/api/v4/projects/42/merge_requests/7", BOB)[0], 404)

    def test_post_note_as_user(self):
        resp = call("POST", "/api/v4/projects/42/merge_requests/7/notes", body=json.dumps({"body": "Wyglada dobrze"}).encode())
        self.assertEqual(resp.status, 201)
        note = json.loads(resp.body)
        self.assertEqual(note["author"]["username"], "alice")
        self.assertEqual(note["body"], "Wyglada dobrze")
        self.assertTrue(note["id"] > 0 and note["created_at"])

    def test_note_is_not_stored(self):
        before = get_json("/api/v4/projects/42/merge_requests/7")[1]
        call("POST", "/api/v4/projects/42/merge_requests/7/notes", body=b'{"body": "x"}')
        self.assertEqual(get_json("/api/v4/projects/42/merge_requests/7")[1], before)

    def test_note_without_body_is_400(self):
        for body in (b"", b"{}", b'{"body": "  "}', b"not json"):
            resp = call("POST", "/api/v4/projects/42/merge_requests/7/notes", body=body)
            self.assertEqual(resp.status, 400, body)

    def test_note_on_missing_merge_request_is_404(self):
        self.assertEqual(call("POST", "/api/v4/projects/42/merge_requests/999/notes", body=b'{"body": "x"}').status, 404)

    def test_note_without_access_is_404(self):
        self.assertEqual(call("POST", "/api/v4/projects/51/merge_requests/1/notes", body=b'{"body": "x"}').status, 404)
        self.assertEqual(call("POST", "/api/v4/projects/42/merge_requests/7/notes", BOB, b'{"body": "x"}').status, 404)

    def test_read_only_member_gets_403(self):
        gitlab_rest.GROUP_ACCESS["bob"] = {"team-a": "read"}
        try:
            self.assertEqual(call("POST", "/api/v4/projects/42/merge_requests/7/notes", BOB, b'{"body": "x"}').status, 403)
            self.assertEqual(get_json("/api/v4/projects/42/merge_requests/7", BOB)[0], 200)
        finally:
            del gitlab_rest.GROUP_ACCESS["bob"]

    def test_wrong_method(self):
        self.assertEqual(call("GET", "/api/v4/projects/42/merge_requests/7/notes").status, 405)
        self.assertEqual(call("POST", "/api/v4/projects/42/merge_requests/7").status, 405)


if __name__ == "__main__":
    unittest.main()
