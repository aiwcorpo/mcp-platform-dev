#!/usr/bin/env python3
"""Latency and rate limits of the system mocks for load tests (mocks/systems/faults.py).

  mock-fault.py show   <system>
  mock-fault.py set    <system> key=value ...      latencyMs=200 jitterMs=50 rateLimitPerSecond=20 burst=40 retryAfterSeconds=2
                                                   errorStatus=503 errorPercent=5 pathPrefix=/api/
  mock-fault.py clear  <system>|all

<system>: gitlab, confluence, servicedesk, opensearch (Python mocks, all settings) or jira (WireMock: latencyMs only, as the
global fixed delay; no rate limit, use a stub mapping through its admin API for a 429). The changes apply to the running
mock and are lost when it restarts; the start values come from the MOCK_* variables of faults.py.
Ports and passwords come from .env and .secrets/dev.env like in the other scripts.
"""
import base64
import json
import sys
import urllib.error
import urllib.request

import mcpdev

PYTHON_MOCKS = ("gitlab", "confluence", "servicedesk", "opensearch")


def call(system, method, path, body=None):
    if system == "jira":
        port, user, password = mcpdev.cfg("JIRA_MOCK_PORT"), "admin", mcpdev.cfg("JIRA_MOCK_ADMIN_PASSWORD")
    else:
        port, user, password = mcpdev.cfg(f"{system.upper()}_MOCK_PORT"), "admin", mcpdev.cfg("MOCK_ADMIN_PASSWORD")
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def parse_settings(pairs):
    settings = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            sys.exit(f"expected key=value, got {pair!r}")
        settings[key] = value if key == "pathPrefix" else float(value) if "." in value else int(value)
    return settings


def show(system):
    if system == "jira":
        status, body = call(system, "GET", "/__admin/settings")
    else:
        status, body = call(system, "GET", "/__mock/faults")
    print(f"{system}: HTTP {status} {body.decode('utf-8', 'replace')}")
    return status == 200


def apply(system, settings):
    if system == "jira":
        if set(settings) - {"latencyMs"}:
            sys.exit("jira (WireMock) supports latencyMs only")
        status, body = call(system, "POST", "/__admin/settings", {"fixedDelay": settings.get("latencyMs", 0)})
    else:
        status, body = call(system, "PUT", "/__mock/faults", settings)
    print(f"{system}: HTTP {status} {body.decode('utf-8', 'replace')}")
    return status == 200


def main(argv):
    if len(argv) < 2 or argv[0] not in ("show", "set", "clear"):
        print(__doc__)
        return 2
    command, target, rest = argv[0], argv[1], argv[2:]
    systems = ("jira",) + PYTHON_MOCKS if target == "all" and command != "set" else (target,)
    if any(s not in ("jira",) + PYTHON_MOCKS for s in systems):
        sys.exit(f"unknown system {target!r}")
    ok = True
    for system in systems:
        if command == "show":
            ok &= show(system)
        elif command == "clear":
            ok &= apply(system, {}) if system == "jira" else call(system, "DELETE", "/__mock/faults")[0] == 200
        else:
            ok &= apply(system, parse_settings(rest))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
