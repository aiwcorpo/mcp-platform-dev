"""TempUser of scripts/mcpdev.py always deletes its account. The Keycloak admin API and the purge calls are replaced in a subprocess
(importing mcpdev reads the configuration of the stack, so it does not run in this process)."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DRIVER = r'''
import json, signal, os, sys
sys.path.insert(0, %(scripts)r)
import mcpdev
scenario = sys.argv[1]
calls = []
delete_status = 500 if scenario == "delete_fails" else 204
def kc_admin(method, path, body=None):
    calls.append(method + " " + path.split("?")[0])
    if method == "POST" and path == "/users":
        return 201, {}
    if method == "GET" and path.startswith("/users?"):
        return 200, [{"id": "sub-1"}]
    if method == "GET" and path.startswith("/groups"):
        return 200, [] if scenario == "missing_group" else [{"id": "g1", "name": "mcp-jira-read"}]
    if method == "DELETE":
        return delete_status, {}
    return 204, {}
def purge(*args, **kwargs):
    if scenario == "no_certificate":
        raise FileNotFoundError("certs/mcp-offboarding/tls.crt")
    return 204, {}
mcpdev.kc_admin = kc_admin
mcpdev.offboarding_request = purge
mcpdev.gateway_request = purge
outcome = "ok"
try:
    with mcpdev.TempUser(["mcp-jira-read"], otp=False) as user:
        if scenario == "sigterm":
            os.kill(os.getpid(), signal.SIGTERM)
        if scenario == "error":
            raise RuntimeError("test failed")
except SystemExit as e:
    outcome = "exit %%s" %% e.code
except Exception as e:
    outcome = type(e).__name__ + ": " + str(e)
print(json.dumps({"outcome": outcome, "deleted": "DELETE /users/sub-1" in calls}))
'''


def run(scenario):
    done = subprocess.run([sys.executable, "-I", "-c", DRIVER % {"scripts": str(ROOT / "scripts")}, scenario], capture_output=True, text=True, cwd=ROOT)
    return json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {"outcome": done.stderr[-300:], "deleted": None}


class TempUserTest(unittest.TestCase):
    def test_deleted_after_a_normal_run(self):
        self.assertEqual({"outcome": "ok", "deleted": True}, run("normal"))

    def test_deleted_after_an_error_in_the_test(self):
        result = run("error")
        self.assertTrue(result["deleted"])
        self.assertIn("test failed", result["outcome"])

    def test_deleted_when_a_group_is_missing(self):
        result = run("missing_group")
        self.assertTrue(result["deleted"], result)
        self.assertIn("does not exist in the realm", result["outcome"])

    def test_deleted_without_the_offboarding_certificate(self):
        self.assertEqual({"outcome": "ok", "deleted": True}, run("no_certificate"))

    def test_deleted_on_sigterm(self):
        result = run("sigterm")
        self.assertTrue(result["deleted"], result)
        self.assertEqual("exit 143", result["outcome"])

    def test_a_failed_delete_is_an_error_that_names_the_account(self):
        result = run("delete_fails")
        self.assertIn("was NOT deleted", result["outcome"])
        self.assertIn("m4-", result["outcome"])


if __name__ == "__main__":
    unittest.main()
