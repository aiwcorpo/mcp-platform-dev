import base64
import http.client
import json
import os
import random
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common  # noqa: E402
import faults  # noqa: E402
import server  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 100.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)


def make(clock=None, **settings):
    clock = clock or Clock()
    f = faults.Faults(clock=clock, sleep=clock.sleep, rng=random.Random(1))
    f.configure(settings)
    return f, clock


def get(path):
    return common.Request("GET", path, {}, b"")


class FaultsTest(unittest.TestCase):
    def test_nothing_configured_changes_nothing(self):
        f, clock = make()
        self.assertIsNone(f.apply(get("/rest/api/2/myself")))
        self.assertEqual(clock.slept, [])

    def test_latency_is_applied_before_the_handler(self):
        f, clock = make(latencyMs=250)
        self.assertIsNone(f.apply(get("/x")))
        self.assertEqual(clock.slept, [0.25])

    def test_jitter_stays_within_bounds(self):
        f, clock = make(latencyMs=100, jitterMs=50)
        for _ in range(200):
            f.apply(get("/x"))
        self.assertTrue(all(0.1 <= s <= 0.15 for s in clock.slept))
        self.assertGreater(max(clock.slept) - min(clock.slept), 0.02)

    def test_rate_limit_answers_429_with_retry_after_once_the_burst_is_used(self):
        f, clock = make(rateLimitPerSecond=2, burst=3, retryAfterSeconds=7)
        self.assertEqual([f.apply(get("/x")) is None for _ in range(3)], [True, True, True])
        limited = f.apply(get("/x"))
        self.assertEqual(limited.status, 429)
        self.assertEqual(limited.headers["Retry-After"], "7")
        self.assertEqual(json.loads(limited.body), {"error": "rate_limited"})

    def test_bucket_refills_with_time(self):
        f, clock = make(rateLimitPerSecond=2, burst=2)
        f.apply(get("/x"))
        f.apply(get("/x"))
        self.assertEqual(f.apply(get("/x")).status, 429)
        clock.now += 0.5
        self.assertIsNone(f.apply(get("/x")))
        self.assertEqual(f.apply(get("/x")).status, 429)
        clock.now += 60
        self.assertEqual([f.apply(get("/x")) is None for _ in range(3)], [True, True, False])

    def test_paths_outside_the_prefix_are_untouched(self):
        f, clock = make(rateLimitPerSecond=1, burst=1, latencyMs=10, pathPrefix="/api/")
        self.assertIsNone(f.apply(get("/oauth/token")))
        self.assertIsNone(f.apply(get("/oauth/token")))
        self.assertEqual(clock.slept, [])
        self.assertIsNone(f.apply(get("/api/v4/user")))
        self.assertEqual(f.apply(get("/api/v4/user")).status, 429)

    def test_injected_errors_respect_the_percentage(self):
        f, _ = make(errorStatus=503, errorPercent=100)
        self.assertEqual(f.apply(get("/x")).status, 503)
        f, _ = make(errorStatus=503, errorPercent=0)
        self.assertIsNone(f.apply(get("/x")))

    def test_a_limited_request_does_not_count_as_an_injected_failure(self):
        f, _ = make(rateLimitPerSecond=1, burst=1, errorStatus=500, errorPercent=100)
        self.assertEqual(f.apply(get("/x")).status, 500)
        self.assertEqual(f.apply(get("/x")).status, 429)
        self.assertEqual(f.state()["counters"], {"delayed": 0, "limited": 1, "failed": 1})

    def test_invalid_settings_are_refused_and_keep_the_old_ones(self):
        f, _ = make(latencyMs=5)
        for bad in ({"latencyMs": -1}, {"latencyMs": 60001}, {"latencyMs": 1.5}, {"latencyMs": True}, {"nope": 1},
                    {"errorStatus": 404}, {"errorPercent": 101}, {"pathPrefix": "api"}, ["latencyMs"]):
            with self.assertRaises(faults.FaultsError, msg=str(bad)):
                f.configure(bad)
        self.assertEqual(f.state()["settings"], {"latencyMs": 5})

    def test_environment_sets_the_initial_state(self):
        f = faults.Faults.from_env({"MOCK_LATENCY_MS": "40", "MOCK_RATE_LIMIT_PER_SECOND": "2.5", "MOCK_FAULT_PATH_PREFIX": "/api/"})
        self.assertEqual(f.state()["settings"], {"latencyMs": 40, "rateLimitPerSecond": 2.5, "pathPrefix": "/api/"})
        with self.assertRaises(faults.FaultsError):
            faults.Faults.from_env({"MOCK_LATENCY_MS": "slow"})


class ControlEndpointTest(unittest.TestCase):
    PASSWORD = "control-test"

    @classmethod
    def setUpClass(cls):
        os.environ["MOCK_SYSTEM"] = "confluence"
        cls.faults = faults.Faults()
        handler = server.make_handler("confluence", common.Journal(), cls.PASSWORD, cls.faults)
        handler.log_message = lambda *args: None
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method, path, body=None, auth=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Basic " + base64.b64encode(("admin:" + self.PASSWORD).encode()).decode()
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), data

    def tearDown(self):
        self.faults.clear()

    def test_control_needs_the_admin_password(self):
        self.assertEqual(self.call("GET", "/__mock/faults", auth=False)[0], 401)
        self.assertEqual(self.call("PUT", "/__mock/faults", {"latencyMs": 1}, auth=False)[0], 401)
        self.assertEqual(self.faults.state()["settings"], {})

    def test_put_get_delete_and_the_effect_on_requests(self):
        status, _, body = self.call("PUT", "/__mock/faults", {"rateLimitPerSecond": 1, "burst": 1, "retryAfterSeconds": 3})
        self.assertEqual((status, json.loads(body)["settings"]["burst"]), (200, 1))
        first = self.call("GET", "/rest/api/content/1")[0]
        status, headers, _ = self.call("GET", "/rest/api/content/1")
        self.assertNotEqual(first, 429)
        self.assertEqual((status, headers.get("Retry-After")), (429, "3"))
        self.assertEqual(self.call("GET", "/__mock/health", auth=False)[0], 200)
        self.assertEqual(json.loads(self.call("GET", "/__mock/faults")[2])["counters"]["limited"], 1)
        self.call("DELETE", "/__mock/faults")
        self.assertNotEqual(self.call("GET", "/rest/api/content/1")[0], 429)

    def test_bad_settings_are_400(self):
        status, _, body = self.call("PUT", "/__mock/faults", {"latencyMs": "slow"})
        self.assertEqual((status, json.loads(body)["error"]), (400, "invalid_settings"))
        self.assertEqual(self.call("POST", "/__mock/faults", {})[0], 405)


if __name__ == "__main__":
    unittest.main()
