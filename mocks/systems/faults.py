"""Controlled latency and rate limiting for the system mocks (load tests, M5).

One Faults object per mock process. It is configured from the environment at start and at run time through
PUT /__mock/faults (Basic auth like the journal); GET shows the current settings and counters, DELETE switches
everything off. Nothing is applied to /__mock/ paths.

Settings (all optional, zero = off):
  latencyMs, jitterMs     every request waits latencyMs plus a uniform 0..jitterMs before the handler runs
  rateLimitPerSecond      token bucket over all matching requests of the process; the excess gets 429 with Retry-After
  burst                   bucket size (default: the rate rounded up, at least 1)
  retryAfterSeconds       value of Retry-After (default 1)
  errorStatus, errorPercent   answer errorStatus (500..599) to this percentage of the requests that passed the bucket
  pathPrefix              only paths starting with it are affected (default "/")
Environment at start: MOCK_LATENCY_MS, MOCK_LATENCY_JITTER_MS, MOCK_RATE_LIMIT_PER_SECOND, MOCK_RATE_LIMIT_BURST,
MOCK_RATE_LIMIT_RETRY_AFTER, MOCK_ERROR_STATUS, MOCK_ERROR_PERCENT, MOCK_FAULT_PATH_PREFIX.
"""
import math
import random
import threading
import time

import common

MAX_LATENCY_MS = 60000
_FIELDS = {
    "latencyMs": (int, 0, MAX_LATENCY_MS),
    "jitterMs": (int, 0, MAX_LATENCY_MS),
    "rateLimitPerSecond": (float, 0, 100000),
    "burst": (int, 0, 100000),
    "retryAfterSeconds": (int, 0, 3600),
    "errorStatus": (int, 0, 599),
    "errorPercent": (float, 0, 100),
    "pathPrefix": (str, None, None),
}
_ENV = {
    "MOCK_LATENCY_MS": "latencyMs", "MOCK_LATENCY_JITTER_MS": "jitterMs",
    "MOCK_RATE_LIMIT_PER_SECOND": "rateLimitPerSecond", "MOCK_RATE_LIMIT_BURST": "burst",
    "MOCK_RATE_LIMIT_RETRY_AFTER": "retryAfterSeconds", "MOCK_ERROR_STATUS": "errorStatus",
    "MOCK_ERROR_PERCENT": "errorPercent", "MOCK_FAULT_PATH_PREFIX": "pathPrefix",
}


class FaultsError(ValueError):
    pass


def _clean(raw):
    if not isinstance(raw, dict):
        raise FaultsError("settings must be a JSON object")
    out = {}
    for key, value in raw.items():
        if key not in _FIELDS:
            raise FaultsError("unknown setting " + key)
        kind, low, high = _FIELDS[key]
        if kind is str:
            if not isinstance(value, str) or not value.startswith("/"):
                raise FaultsError("pathPrefix must be a string starting with /")
            out[key] = value
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (kind is int and value != int(value)):
            raise FaultsError(key + " must be a " + ("whole " if kind is int else "") + "number")
        if not low <= value <= high:
            raise FaultsError("%s must be between %s and %s" % (key, low, high))
        out[key] = kind(value)
    if out.get("errorStatus", 0) and not 500 <= out["errorStatus"] <= 599:
        raise FaultsError("errorStatus must be 0 or 500..599")
    return out


class Faults:
    def __init__(self, clock=time.monotonic, sleep=time.sleep, rng=None):
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._settings = {}
        self._tokens = 0.0
        self._refilled_at = clock()
        self._counters = {"delayed": 0, "limited": 0, "failed": 0}

    @classmethod
    def from_env(cls, environ, **kwargs):
        faults = cls(**kwargs)
        raw = {}
        for name, key in _ENV.items():
            if environ.get(name):
                kind = _FIELDS[key][0]
                try:
                    raw[key] = kind(environ[name]) if kind is not int else int(environ[name])
                except ValueError:
                    raise FaultsError(name + " is not a valid value") from None
        faults.configure(raw)
        return faults

    def configure(self, raw):
        settings = _clean(raw)
        with self._lock:
            self._settings = settings
            self._tokens = float(self._burst(settings))
            self._refilled_at = self._clock()
            self._counters = {"delayed": 0, "limited": 0, "failed": 0}

    def clear(self):
        self.configure({})

    def state(self):
        with self._lock:
            return {"settings": dict(self._settings), "counters": dict(self._counters)}

    @staticmethod
    def _burst(settings):
        rate = settings.get("rateLimitPerSecond", 0)
        return settings.get("burst") or (max(1, math.ceil(rate)) if rate else 0)

    def apply(self, req):
        """Waits as configured and returns a 429 or error Response for the request, or None to let the handler run."""
        with self._lock:
            settings = self._settings
            if not settings or not req.raw_path.startswith(settings.get("pathPrefix", "/")):
                return None
            delay_ms = settings.get("latencyMs", 0)
            if settings.get("jitterMs"):
                delay_ms += self._rng.uniform(0, settings["jitterMs"])
            verdict = None
            rate = settings.get("rateLimitPerSecond", 0)
            if rate:
                now = self._clock()
                self._tokens = min(float(self._burst(settings)), self._tokens + (now - self._refilled_at) * rate)
                self._refilled_at = now
                if self._tokens >= 1:
                    self._tokens -= 1
                else:
                    self._counters["limited"] += 1
                    verdict = common.json_response(429, {"error": "rate_limited"},
                                                   {"Retry-After": str(settings.get("retryAfterSeconds", 1))})
            if verdict is None and settings.get("errorStatus") and self._rng.uniform(0, 100) < settings.get("errorPercent", 100):
                self._counters["failed"] += 1
                verdict = common.json_response(settings["errorStatus"], {"error": "injected_failure"})
            if delay_ms:
                self._counters["delayed"] += 1
        if delay_ms:
            self._sleep(delay_ms / 1000.0)
        return verdict
