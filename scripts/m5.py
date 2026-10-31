#!/usr/bin/env python3
"""Helpers of scripts/apply-m5-switch.sh that need more than a shell line. They read metadata only: no secret, no token, no seed.

  m5.py bundle-latest                     print the revision of the latest main bundle (exit 1 when it cannot be read)
  m5.py bundle-wait REVISION SECONDS [MAJOR]   wait for a main bundle other than REVISION whose manifest schemaVersion starts with MAJOR (default 5.)
  m5.py connections snapshot FILE         counts of broker connections per user, system and state (alice, bob, carol, mallory)
  m5.py connections compare FILE          the same now, compared with FILE; exit 1 when a count differs
  m5.py endpoint-check                    /mcp through the public origin: 401 challenge, metadata without query, /v/ gone
"""
import io
import json
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import mcpdev

ROOT = Path(__file__).resolve().parents[1]
POLICIES = f"http://localhost:{mcpdev.cfg('POLICIES_PORT')}/policies/main"
PROXY = f"https://localhost:{mcpdev.cfg('PROXY_TLS_PORT')}"
USERS = ("alice", "bob", "carol", "mallory")


def get(url, context=None, method="GET", headers=None):
    request = urllib.request.Request(url, method=method, headers=headers or {}, data=b"{}" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=15, context=context) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def latest():
    status, _, raw = get(f"{POLICIES}/latest.json")
    if status != 200:
        raise OSError(f"latest.json: HTTP {status}")
    return json.loads(raw)


def bundle_schema_version(info):
    status, _, raw = get(f"{POLICIES}/{info['url']}")
    if status != 200:
        raise OSError(f"bundle: HTTP {status}")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        return json.load(tar.extractfile("manifest.json")).get("schemaVersion", "")


def bundle_latest():
    revision = latest().get("revision", "")
    if not revision:
        raise OSError("latest.json has no revision")
    print(revision)


def bundle_wait(before, seconds, major="5."):
    deadline = time.time() + seconds
    seen = ""
    while time.time() < deadline:
        try:
            info = latest()
            if info.get("revision") and info["revision"] != before:
                seen = bundle_schema_version(info)
                if seen.startswith(major):
                    print(f"bundle {info['revision']} schemaVersion {seen}")
                    return 0
        except (OSError, KeyError, ValueError, tarfile.TarError):
            pass
        time.sleep(5)
    print(f"no main bundle other than {before} with schemaVersion {major}x (last schemaVersion seen: {seen or 'none'})", file=sys.stderr)
    return 1


def connection_counts():
    subs = {}
    for name in USERS:
        _, found = mcpdev.kc_admin("GET", f"/users?username={name}&exact=true")
        subs[found[0]["id"]] = name
    quoted = ",".join("'" + sub + "'" for sub in subs)
    out = subprocess.run([str(ROOT / "scripts" / "compose.sh"), "--profile", "full", "--profile", "m2", "exec", "-T", "postgres", "psql", "-U", "broker",
                          "-d", "broker", "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c",
                          f"select sub, system, state, count(*) from connection where sub in ({quoted}) group by 1,2,3 order by 1,2,3"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise OSError("psql failed: " + out.stderr.strip()[:200])
    counts = {name: {} for name in USERS}
    for line in out.stdout.splitlines():
        sub, system, state, count = line.split("|")
        counts[subs[sub]][f"{system}/{state}"] = int(count)
    return counts


def connections(action, file):
    now = connection_counts()
    if action == "snapshot":
        Path(file).write_text(json.dumps(now, indent=1, sort_keys=True))
        print("connections:", {user: sum(v.values()) for user, v in now.items()})
        return 0
    before = json.loads(Path(file).read_text())
    if before != now:
        for user in USERS:
            if before.get(user) != now.get(user):
                print(f"connections of {user} changed: {before.get(user)} -> {now.get(user)}", file=sys.stderr)
        return 1
    print("connections unchanged:", {user: sum(v.values()) for user, v in now.items()})
    return 0


def endpoint_check():
    context = ssl.create_default_context(cafile=str(ROOT / "certs" / "public-ca" / "ca.crt"))
    failures = []
    status, headers, _ = get(f"{PROXY}/mcp?systems=jira", context, "POST")
    challenge = headers.get("WWW-Authenticate", "")
    if status != 401 or f'resource_metadata="{PROXY}/.well-known/oauth-protected-resource/mcp"' not in challenge:
        failures.append(f"POST /mcp without a token: HTTP {status} {challenge[:120]}")
    status, _, raw = get(f"{PROXY}/.well-known/oauth-protected-resource/mcp?systems=jira", context)
    if status != 200 or json.loads(raw).get("resource") != f"{PROXY}/mcp":
        failures.append(f"metadata of /mcp: HTTP {status} {raw[:100]!r}")
    status, _, raw = get(f"{PROXY}/v/x/mcp", context, "POST")
    if status != 404 or json.loads(raw).get("error") != "not_found_or_denied":
        failures.append(f"/v/ must be 404 not_found_or_denied: HTTP {status} {raw[:100]!r}")
    for failure in failures:
        print("endpoint-check: " + failure, file=sys.stderr)
    if not failures:
        print("endpoint-check: /mcp challenge, metadata without query and the gone /v/ are as the contract says")
    return 1 if failures else 0


def main(argv):
    try:
        if argv[:1] == ["bundle-latest"]:
            return bundle_latest() or 0
        if argv[:1] == ["bundle-wait"] and len(argv) in (3, 4):
            return bundle_wait(argv[1], int(argv[2]), *argv[3:])
        if argv[:2] in (["connections", "snapshot"], ["connections", "compare"]) and len(argv) == 3:
            return connections(argv[1], argv[2])
        if argv == ["endpoint-check"]:
            return endpoint_check()
    except (OSError, KeyError, ValueError, mcpdev.Fail) as err:
        print(f"m5: {type(err).__name__}: {err}", file=sys.stderr)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
