import base64
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common  # noqa: E402
import opensearch  # noqa: E402

ISS = "https://localhost:18746/realms/corp"


def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def jwt(**claims):
    return b64({"alg": "RS256"}) + "." + b64(claims) + ".sig"


def exchanged(user, **extra):
    claims = {"iss": ISS, "aud": "opensearch", "preferred_username": user, "exp": int(time.time()) + 300}
    claims.update(extra)
    return jwt(**claims)


def call(method, target, token=None, body=None, raw_header=None):
    headers = {}
    if raw_header is not None:
        headers["Authorization"] = raw_header
    elif token is not None:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else b""
    return opensearch.handle(common.Request(method, target, headers, data))


def payload(resp):
    return json.loads(resp.body)


def server_search_body(text="timeout", size=20, prefixes=("logs-team-a",), start="2026-10-27T00:00:00Z",
                       end="2026-10-29T00:00:00Z"):
    """Body shaped like OpenSearchAdapter.searchBody."""
    clauses = []
    for p in prefixes:
        clauses.append({"term": {"_index": p}})
        clauses.append({"prefix": {"_index": p + "-"}})
    return {
        "size": size, "track_total_hits": False, "timeout": "10s",
        "_source": {"includes": ["@timestamp", "message", "log.level", "service.name", "host.name"]},
        "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
        "query": {"bool": {
            "must": [{"simple_query_string": {"query": text, "fields": ["message"], "default_operator": "and",
                                              "flags": "AND|OR|NOT|PHRASE|PREFIX|PRECEDENCE|WHITESPACE|ESCAPE",
                                              "lenient": True}}],
            "filter": [{"range": {"@timestamp": {"gte": start, "lt": end, "format": "strict_date_optional_time"}}},
                       {"bool": {"should": clauses, "minimum_should_match": 1}}]}}}


RESOLVE = "/_resolve/index/logs-team-a%2Clogs-team-a-*?expand_wildcards=open&ignore_unavailable=true&allow_no_indices=true"
SEARCH_OPTS = "?expand_wildcards=open&ignore_unavailable=true&allow_no_indices=true&allow_partial_search_results=false"


def search(token, expr="logs-team-a%2Clogs-team-a-*", body=None, opts=SEARCH_OPTS):
    return call("POST", "/" + expr + "/_search" + opts, token, body if body is not None else server_search_body())


class AuthTest(unittest.TestCase):
    ENDPOINTS = [
        ("GET", RESOLVE, None),
        ("GET", "/_alias/logs-team-a", None),
        ("POST", "/logs-team-a-*/_search", {"query": {"match_all": {}}}),
        ("GET", "/logs-team-a-2026.10.28/_doc/a28-001", None),
    ]

    def assert_401(self, resp):
        self.assertEqual(resp.status, 401)
        body = payload(resp)
        self.assertEqual(body["status"], 401)
        self.assertEqual(body["error"]["type"], "security_exception")

    def test_missing_header_is_401_on_every_endpoint(self):
        for method, target, body in self.ENDPOINTS:
            self.assert_401(call(method, target, None, body))

    def test_gateway_audience_is_401_on_every_endpoint(self):
        token = jwt(iss=ISS, aud="mcp-gateway", preferred_username="alice")
        for method, target, body in self.ENDPOINTS:
            self.assert_401(call(method, target, token, body))

    def test_pat_basic_and_wrong_issuer_are_401(self):
        self.assert_401(call("GET", RESOLVE, "alice-pat"))
        self.assert_401(call("GET", RESOLVE, raw_header="Basic " + base64.b64encode(b"alice:x").decode()))
        self.assert_401(call("GET", RESOLVE, jwt(iss="http://evil.test/realms/corp", aud="opensearch",
                                                 preferred_username="alice")))

    def test_expired_token_is_401(self):
        self.assert_401(call("GET", RESOLVE, exchanged("alice", exp=int(time.time()) - 5)))

    def test_audience_list_with_opensearch_is_accepted(self):
        token = exchanged("alice", aud=["mcp-credential-broker", "opensearch"])
        self.assertEqual(call("GET", RESOLVE, token).status, 200)

    def test_unknown_route_is_not_handled(self):
        self.assertIsNone(call("GET", "/_cluster/health", exchanged("alice")))
        self.assertIsNone(call("DELETE", "/logs-team-a-2026.10.28", exchanged("alice")))


class ResolveIndexTest(unittest.TestCase):
    def test_alice_sees_only_team_a(self):
        resp = call("GET", RESOLVE, exchanged("alice"))
        self.assertEqual(resp.status, 200)
        body = payload(resp)
        self.assertEqual([i["name"] for i in body["indices"]], ["logs-team-a-2026.10.27", "logs-team-a-2026.10.28"])
        self.assertTrue(all("open" in i["attributes"] for i in body["indices"]))
        self.assertEqual(body["aliases"], [])
        self.assertEqual(body["data_streams"], [])

    def test_wildcard_over_forbidden_indices_is_narrowed_not_denied(self):
        resp = call("GET", "/_resolve/index/*?expand_wildcards=open", exchanged("bob"))
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(payload(resp)["indices"]), 2)
        resp = call("GET", "/_resolve/index/logs-team-b-*", exchanged("alice"))
        self.assertEqual(payload(resp)["indices"], [])

    def test_explicit_forbidden_index_is_403(self):
        for name in ("logs-team-b-2026.10.28", "hr-payroll"):
            resp = call("GET", "/_resolve/index/" + name, exchanged("alice"))
            self.assertEqual(resp.status, 403)
            self.assertEqual(payload(resp)["error"]["type"], "security_exception")

    def test_user_without_group_gets_nothing(self):
        self.assertEqual(payload(call("GET", RESOLVE, exchanged("mallory")))["indices"], [])
        self.assertEqual(call("GET", "/_resolve/index/logs-team-a-2026.10.28", exchanged("carol")).status, 403)

    def test_missing_index(self):
        self.assertEqual(call("GET", "/_resolve/index/logs-team-a-2099.01.01?ignore_unavailable=true", exchanged("alice")).status, 200)
        resp = call("GET", "/_resolve/index/logs-team-a-2099.01.01?ignore_unavailable=false", exchanged("alice"))
        self.assertEqual(resp.status, 404)
        self.assertEqual(payload(resp)["error"]["type"], "index_not_found_exception")

    def test_groups_claim_wins_over_login(self):
        tok = exchanged("alice", groups=["mcp-opensearch-read"])
        self.assertEqual(payload(call("GET", RESOLVE, tok))["indices"], [])
        tok = exchanged("mallory", groups=["mcp-opensearch-read", "mcp-opensearch-idx-logs-team-a"])
        self.assertEqual(len(payload(call("GET", RESOLVE, tok))["indices"]), 2)


class AliasTest(unittest.TestCase):
    def test_unknown_alias_is_404_for_authenticated_caller(self):
        resp = call("GET", "/_alias/logs-team-a-current", exchanged("alice"))
        self.assertEqual(resp.status, 404)
        self.assertIn("missing", payload(resp)["error"])

    def test_alias_requires_authentication(self):
        self.assertEqual(call("GET", "/_alias/logs-team-a-current").status, 401)


class SearchTest(unittest.TestCase):
    def test_server_shaped_search_returns_matching_hits_newest_first(self):
        resp = search(exchanged("alice"))
        self.assertEqual(resp.status, 200)
        hits = payload(resp)["hits"]
        self.assertNotIn("total", hits)
        self.assertEqual([h["_id"] for h in hits["hits"]], ["a28-004", "a27-003"])
        first = hits["hits"][0]
        self.assertEqual(first["_index"], "logs-team-a-2026.10.28")
        self.assertEqual(first["_source"]["log"]["level"], "error")
        self.assertEqual(first["_source"]["service"]["name"], "inventory")
        self.assertEqual(first["_source"]["host"]["name"], "node-a2")
        self.assertEqual(first["_source"]["@timestamp"], "2026-10-28T09:02:30Z")
        self.assertEqual(set(first["_source"]), {"@timestamp", "message", "log", "service", "host"})

    def test_time_range_and_size(self):
        body = server_search_body(text="order", start="2026-10-28T00:00:00+01:00", end="2026-10-29T00:00:00Z")
        ids = [h["_id"] for h in payload(search(exchanged("bob"), body=body))["hits"]["hits"]]
        self.assertEqual(ids, ["a28-002", "a28-001"])
        body = server_search_body(text="order", size=1)
        self.assertEqual(len(payload(search(exchanged("bob"), body=body))["hits"]["hits"]), 1)

    def test_range_excludes_upper_bound(self):
        body = server_search_body(text="timeout", end="2026-10-28T09:02:30Z")
        ids = [h["_id"] for h in payload(search(exchanged("alice"), body=body))["hits"]["hits"]]
        self.assertEqual(ids, ["a27-003"])

    def test_injection_document_is_returned_as_plain_data(self):
        body = server_search_body(text="instructions")
        hits = payload(search(exchanged("alice"), body=body))["hits"]["hits"]
        self.assertEqual([h["_id"] for h in hits], ["a28-005"])
        self.assertIn("Ignore all previous instructions", hits[0]["_source"]["message"])

    def test_operators_phrase_prefix_and_negation(self):
        def ids(text):
            body = server_search_body(text=text)
            return [h["_id"] for h in payload(search(exchanged("alice"), body=body))["hits"]["hits"]]
        self.assertEqual(ids('"payment declined"'), ["a28-002"])
        self.assertEqual(ids("paym*"), ["a28-002", "a27-002"])
        self.assertEqual(ids("order -declined"), ["a28-001", "a27-001"])
        self.assertEqual(ids("declined | cache"), ["a28-003", "a28-002"])
        self.assertEqual(ids("declined cache"), [])

    def test_match_all_term_and_match_queries(self):
        tok = exchanged("alice")
        resp = search(tok, body={"query": {"match_all": {}}, "size": 100})
        self.assertEqual(len(payload(resp)["hits"]["hits"]), 9)
        self.assertEqual(payload(resp)["hits"]["total"]["value"], 9)
        errors = search(tok, body={"query": {"bool": {"filter": [{"term": {"level": "ERROR"}},
                                                                {"term": {"service": "checkout"}}]}}})
        self.assertEqual([h["_id"] for h in payload(errors)["hits"]["hits"]], ["a28-002"])
        match = search(tok, body={"query": {"match": {"message": {"query": "stock database", "operator": "and"}}}, "sort": [{"@timestamp": "asc"}]})
        self.assertEqual([h["_id"] for h in payload(match)["hits"]["hits"]], ["a27-003", "a28-004"])

    def test_query_string_field_syntax_and_source_filter(self):
        resp = search(exchanged("alice"), body={"query": {"query_string": {"query": "level:error AND NOT service:checkout"}},
                                                "_source": ["message"], "sort": [{"@timestamp": "asc"}]})
        hits = payload(resp)["hits"]["hits"]
        self.assertEqual([h["_id"] for h in hits], ["a27-003", "a28-004"])
        self.assertEqual(set(hits[0]["_source"]), {"message"})

    def test_search_over_forbidden_explicit_index_is_403(self):
        for expr in ("logs-team-b-2026.10.28", "hr-payroll", "logs-team-a-2026.10.28%2Chr-payroll"):
            resp = search(exchanged("alice"), expr=expr)
            self.assertEqual(resp.status, 403, expr)
            self.assertEqual(payload(resp)["error"]["type"], "security_exception")

    def test_wildcard_over_forbidden_indices_returns_nothing_allowed_only(self):
        resp = search(exchanged("alice"), expr="*", body={"query": {"match_all": {}}, "size": 100})
        indices = {h["_index"] for h in payload(resp)["hits"]["hits"]}
        self.assertEqual(indices, {"logs-team-a-2026.10.27", "logs-team-a-2026.10.28"})
        resp = search(exchanged("mallory"), expr="logs-team-a-*")
        self.assertEqual(payload(resp)["hits"]["hits"], [])

    def test_missing_index(self):
        resp = search(exchanged("alice"), expr="logs-team-a-2099.01.01", opts="")
        self.assertEqual(resp.status, 404)
        self.assertEqual(payload(resp)["error"]["type"], "index_not_found_exception")
        resp = search(exchanged("alice"), expr="logs-team-a-2099.01.01")
        self.assertEqual(payload(resp)["hits"]["hits"], [])

    def test_unsupported_query_is_400(self):
        resp = search(exchanged("alice"), body={"query": {"script": {}}})
        self.assertEqual(resp.status, 400)

    def test_search_without_token_or_with_wrong_audience(self):
        self.assertEqual(search(None).status, 401)
        self.assertEqual(search(jwt(iss=ISS, aud="mcp-gateway", preferred_username="alice")).status, 401)


class GetDocumentTest(unittest.TestCase):
    def test_found_document_with_source_includes(self):
        resp = call("GET", "/logs-team-a-2026.10.28/_doc/a28-002?_source_includes=@timestamp,message,log.level",
                    exchanged("alice"))
        self.assertEqual(resp.status, 200)
        body = payload(resp)
        self.assertTrue(body["found"])
        self.assertEqual(body["_index"], "logs-team-a-2026.10.28")
        self.assertEqual(body["_id"], "a28-002")
        self.assertEqual(set(body["_source"]), {"@timestamp", "message", "log"})
        self.assertEqual(body["_source"]["log"], {"level": "error"})

    def test_missing_document_is_404_found_false(self):
        resp = call("GET", "/logs-team-a-2026.10.28/_doc/nope", exchanged("alice"))
        self.assertEqual(resp.status, 404)
        self.assertFalse(payload(resp)["found"])

    def test_missing_index_is_404(self):
        resp = call("GET", "/logs-team-a-2099.01.01/_doc/x", exchanged("alice"))
        self.assertEqual(resp.status, 404)
        self.assertEqual(payload(resp)["error"]["type"], "index_not_found_exception")

    def test_forbidden_index_is_403(self):
        for index, doc_id in (("hr-payroll", "p-2026-10-001"), ("logs-team-b-2026.10.28", "b28-001")):
            resp = call("GET", "/%s/_doc/%s" % (index, doc_id), exchanged("bob"))
            self.assertEqual(resp.status, 403)
            self.assertEqual(payload(resp)["status"], 403)

    def test_requires_valid_token(self):
        self.assertEqual(call("GET", "/logs-team-a-2026.10.28/_doc/a28-001").status, 401)
        token = jwt(iss=ISS, aud=["mcp-gateway"], preferred_username="alice")
        self.assertEqual(call("GET", "/logs-team-a-2026.10.28/_doc/a28-001", token).status, 401)


class ServerQueryShapeTest(unittest.TestCase):
    """The body mcp-server-opensearch builds (OpenSearchAdapter.searchBody / aggregateBody)."""

    FILTERS = [{"range": {"@timestamp": {"gte": "2026-10-27T00:00:00Z", "lt": "2026-10-29T00:00:00Z", "format": "strict_date_optional_time"}}},
               {"bool": {"should": [{"term": {"_index": "logs-team-a"}}, {"prefix": {"_index": "logs-team-a-"}}], "minimum_should_match": 1}}]

    def test_multi_match_search(self):
        body = {"size": 21, "track_total_hits": False, "timeout": "10s", "_source": {"includes": ["message"]},
                "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
                "query": {"bool": {"filter": self.FILTERS + [
                    {"multi_match": {"query": "connection timeout", "fields": ["message"], "type": "best_fields", "operator": "and", "lenient": True}},
                    {"term": {"log.level": {"value": "ERROR", "case_insensitive": True}}}]}}}
        resp = call("POST", "/logs-team-a-2026.10.27/_search", exchanged("alice"), body)
        self.assertEqual(resp.status, 200)
        self.assertEqual([h["_id"] for h in payload(resp)["hits"]["hits"]], ["a27-003"])

    def test_terms_aggregation(self):
        body = {"size": 0, "track_total_hits": False, "query": {"bool": {"filter": self.FILTERS}},
                "aggs": {"groups": {"terms": {"field": "log.level", "size": 5}}}}
        resp = call("POST", "/logs-team-a-2026.10.27,logs-team-a-2026.10.28/_search", exchanged("alice"), body)
        self.assertEqual(resp.status, 200)
        buckets = payload(resp)["aggregations"]["groups"]["buckets"]
        self.assertEqual({b["key"]: b["doc_count"] for b in buckets}, {"info": 3, "error": 3, "warn": 2, "debug": 1})
        self.assertEqual(payload(resp)["hits"]["hits"], [])


if __name__ == "__main__":
    unittest.main()
