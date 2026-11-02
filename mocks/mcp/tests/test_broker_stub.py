import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import broker_stub  # noqa: E402
import mcp_stub  # noqa: E402
from test_mcp_stub import NOW, FixedKeys, service_claims, token, user_claims  # noqa: E402

ADMIN = "admin-token-0123456789"


def make_broker(**changes):
    settings = mcp_stub.Settings()
    return broker_stub.BrokerStub(settings, FixedKeys(), ADMIN, clock=lambda: NOW + 1, **changes)


def post(broker, path, claims, body=None, rid="rid-1"):
    status, _, out = broker.handle("POST", path, {"Authorization": "Bearer " + token(claims), "X-Request-Id": rid},
                                   json.dumps(body or {"system": "jira"}).encode())
    return status, json.loads(out)


def admin(broker, method, body=None, bearer=ADMIN):
    status, _, out = broker.admin(method, "/type", {"Authorization": "Bearer " + bearer}, json.dumps(body).encode() if body else b"")
    return status, json.loads(out)


class BrokerStubTest(unittest.TestCase):
    def test_systems_list_is_served_to_the_service_variant(self):
        broker = make_broker()
        status, _, out = broker.handle("GET", "/v1/systems", {"Authorization": "Bearer " + token(service_claims("mcp-credential-broker", "j1")),
                                                              "X-Request-Id": "rid-1"}, b"")
        self.assertEqual((200, "jira", ["user-secret"]), (status, json.loads(out)[0]["system"], json.loads(out)[0]["supportedTypes"]))

    def test_resolve_returns_the_configured_type(self):
        broker = make_broker()
        status, body = post(broker, "/v1/credentials/resolve", user_claims("mcp-credential-broker", "j1", tool="search_issues"))
        self.assertEqual((200, "user-secret", 0), (status, body["type"], body["cacheTtlSeconds"]))
        status, changed = admin(broker, "PUT", {"type": "bogus"})
        self.assertEqual((200, "bogus"), (status, changed["type"]))
        status, body = post(broker, "/v1/credentials/resolve", user_claims("mcp-credential-broker", "j2", tool="search_issues"))
        self.assertEqual("bogus", body["type"])
        self.assertEqual(2, admin(broker, "GET")[1]["resolveCalls"])

    def test_resolve_needs_the_tool_claim_and_the_broker_audience(self):
        broker = make_broker()
        self.assertEqual("tool_missing", post(broker, "/v1/credentials/resolve", user_claims("mcp-credential-broker", "j1"))[1]["reason"])
        self.assertEqual("wrong_audience", post(broker, "/v1/credentials/resolve", user_claims("mcp-jira", "j2", tool="search_issues"))[1]["reason"])

    def test_unknown_system_is_not_configured(self):
        broker = make_broker()
        status, body = post(broker, "/v1/credentials/resolve", user_claims("mcp-credential-broker", "j1", tool="get_page"), {"system": "confluence"})
        self.assertEqual((422, "system_not_configured"), (status, body["error"]))

    def test_admin_needs_the_token_and_a_sane_value(self):
        broker = make_broker()
        self.assertEqual(401, admin(broker, "GET", bearer="wrong")[0])
        self.assertEqual(400, admin(broker, "PUT", {"type": ""})[0])
        self.assertEqual(400, admin(broker, "PUT", {"type": "x" * 65})[0])
        self.assertEqual(400, admin(broker, "PUT", {"type": 3})[0])
        self.assertEqual("user-secret", admin(broker, "GET")[1]["type"])


if __name__ == "__main__":
    unittest.main()
