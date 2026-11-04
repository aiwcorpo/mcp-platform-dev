#!/usr/bin/env python3
"""Checks of profile e2e-prod against its own gateway (outside devstack): the four cases of scenario XH and a list of 250 tools.
Starts the project when it is down and leaves it running. Uses a throw-away Keycloak user (never alice, bob, carol, mallory).
Exit code 0 when every check passed.
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import mcpdev  # noqa: E402

SCRIPT = str(ROOT / "scripts" / "e2e-prod.sh")
GATEWAY = "http://localhost:" + next(line.split("=", 1)[1] for line in (ROOT / "e2e-prod.env").read_text().splitlines()
                                     if line.startswith("E2E_PROD_GATEWAY_PORT="))
failures = []


def check(name, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}  {name}{'  (' + detail + ')' if detail and not condition else ''}")
    if not condition:
        failures.append(name)


def script(*args, check_status=True):
    done = subprocess.run([SCRIPT, *args], capture_output=True, text=True)
    if check_status and done.returncode != 0:
        print(done.stdout[-400:], done.stderr[-400:])
    return done


def rpc(token, method, params=None, session=None, ident=1):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if ident is not None:
        body["id"] = ident
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18",
               "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(GATEWAY + "/mcp", data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers.get("Mcp-Session-Id"), response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, None, error.read().decode()


def session_of(token):
    _, session, _ = rpc(token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e-prod-check", "version": "1"}})
    rpc(token, "notifications/initialized", session=session, ident=None)
    return session


def call_search(token, session):
    status, _, text = rpc(token, "tools/call", {"name": "jira__search_issues", "arguments": {"jql": "x"}}, session=session, ident=2)
    result = json.loads(text).get("result", {}) if status == 200 else {}
    return result.get("isError"), (result.get("content") or [{}])[0].get("text", text)


def main():
    info = script("up")
    check("project is up", info.returncode == 0)
    if info.returncode != 0:
        return 1
    status = json.loads(script("register", "none").stdout)
    check("manifest of the server stand-in is registered (production mode accepts it, READ_ONLY tool approved)",
          status.get("httpStatus") == 200 and [t["status"] for t in status["tools"]] == ["approved"], str(status)[:200])

    script("broker-type", "user-secret")
    with mcpdev.TempUser(["mcp-client-claude-code", "mcp-jira-read"], otp=False) as user:
        token = user.token()
        session = session_of(token)
        is_error, text = call_search(token, session)
        check("XH baseline: a call with a credential of the accepted type works", is_error is False and "credential type user-secret" in text, text[:120])
        for wrong in ("service-account", "not-a-type"):
            script("broker-type", wrong)
            is_error, text = call_search(token, session)
            check(f"XH: the broker returns {wrong!r}, the call is refused without reaching the server", is_error is True and "internal_error" in text, text[:120])
        script("broker-type", "user-secret")
        is_error, text = call_search(token, session)
        check("XH: after the type is set back the call works again", is_error is False, text[:120])

    mismatch = json.loads(script("register", "dev").stdout)
    check("XH environment_mismatch: a manifest of environment dev is refused with 422", mismatch.get("httpStatus") == 422
          and mismatch.get("reason") == "environment_mismatch", str(mismatch)[:200])

    probe = script("devkey-probe", check_status=False)
    check("XH dev- keys: the gateway outside devstack refuses to start with a dev- policy key and with a dev- C2 key",
          probe.returncode == 0 and probe.stdout.count("PASS") == 2, probe.stdout[-300:])

    held = script("held-manifest", check_status=False)
    check("XH held manifest: a stored manifest that fails the start checks disables the server at start", held.returncode == 0, held.stdout[-300:])
    script("register", "none")
    script("restart-gateway")

    script("register-bulk", "none")
    bulk = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "bulk-check.py"), "--url", GATEWAY + "/mcp", "--expect", "bulk", "--wait", "60"],
                          capture_output=True, text=True)
    check("G9 prerequisite: 253 tools in pages of 100, 100, 53 at this gateway", bulk.returncode == 0, bulk.stdout[-300:])
    script("register-bulk", "none")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except mcpdev.Fail as failure:
        print(f"FAIL {failure}")
        sys.exit(1)
