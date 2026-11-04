#!/usr/bin/env python3
"""M1 scenarios (F1, A1, A2, A3, A9, A15) against the components of `make up-full`.

Reuses the scenario code of the mcp-e2e repository (read only, found next to this repository or at MCP_E2E_DIR)
and replaces only what depends on host processes: audit and log sources come from container logs, the broker
is stopped and started with docker. Nothing is written to mcp-e2e. Exit code 0 when every scenario passed,
2 when the stack is not usable, 1 otherwise.
"""
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEV = Path(__file__).resolve().parents[1]
E2E = Path(os.environ.get("MCP_E2E_DIR", DEV.parent / "mcp-e2e")) / "e2e"
CONTAINER = {"gateway": "mcp-gateway", "broker": "mcp-credential-broker", "mcp-jira": "mcp-jira"}
PROJECT = "mcp-dev"
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")

if not (E2E / "m1.py").exists():
    sys.exit(f"no {E2E}/m1.py: clone mcp-e2e next to this repository or set MCP_E2E_DIR")
sys.path.insert(0, str(E2E))
import harness as h  # noqa: E402
import m1  # noqa: E402

h.ROOT = DEV.parent
h.DEV = DEV
h.CERTS = DEV / "certs"


def docker_logs(service):
    out = subprocess.run(["docker", "logs", f"{PROJECT}-{service}-1"], capture_output=True, text=True)
    return out.stdout + out.stderr


class ContainerComponent:
    """Stand-in for a host process component: start/stop through docker."""

    def __init__(self, service):
        self.name = service
        self.container = f"{PROJECT}-{service}-1"

    def alive(self):
        out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", self.container],
                             capture_output=True, text=True)
        return out.stdout.strip() == "true"

    def stop(self):
        subprocess.run(["docker", "stop", self.container], capture_output=True, text=True, check=True)

    def start(self):
        subprocess.run(["docker", "start", self.container], capture_output=True, text=True, check=True)
        if not wait_healthy(self.container):
            raise h.Blocked(f"{self.container} did not become healthy after the restart")
        wait_gateway_reaches_broker()


def health_of(container):
    out = subprocess.run(["docker", "inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}", container],
                         capture_output=True, text=True)
    return out.stdout.strip()


def wait_healthy(container, timeout=90):
    """Containers without a health check count as ready once running."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = health_of(container)
        if status in ("healthy", "none"):
            return True
        time.sleep(1)
    return False


def wait_gateway_reaches_broker(timeout=60):
    """The gateway keeps refusing broker calls for a few seconds after the broker returns, so a healthy broker is
    not enough: poll a real tools/call as alice. Best effort (without a stored PAT it cannot succeed)."""
    sys.path.insert(0, str(DEV / "scripts"))
    import json
    import urllib.request
    import mcpdev
    deadline = time.time() + timeout
    try:
        token = mcpdev.login("alice", "claude-code")
    except Exception:
        return False
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "jira__get_issue", "arguments": {"issueKey": "ABC-1"}}}).encode()
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{h.PORTS['gateway']}/s/mcp-jira/mcp", data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                if b"ABC-1" in resp.read():
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def preflight():
    out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
    running = set(out.stdout.split())
    need = [f"{PROJECT}-{s}-1" for s in ("keycloak", "redis", "openbao", "postgres", "jira-mock",
                                          "mcp-gateway", "mcp-credential-broker", "mcp-jira")]
    missing = [n for n in need if n not in running]
    if missing:
        raise h.Blocked("stack is not up (make up-full): " + ", ".join(missing))
    unhealthy = [n for n in need if not wait_healthy(n, timeout=60)]
    if unhealthy:
        raise h.Blocked("not healthy within 60 s: " + ", ".join(unhealthy))
    wait_gateway_reaches_broker(timeout=20)
    for stub in (f"{PROJECT}-broker-mock-1", f"{PROJECT}-gateway-admin-mock-1"):
        if stub in running:
            raise h.Blocked(f"{stub} is running: it shares the alias with the real component")


def patch_e2e():
    def mcp_audit(rid, logdir):
        return h.run(["python3", str(DEV / "scripts" / "mcpdev.py"), "audit", rid, "--compose"])

    m1.mcp_audit = mcp_audit
    m1.log_text = lambda logdir, name: docker_logs(CONTAINER[name])


def leak_all_containers(ctx):
    """A9 extension: PAT names, the IdP token of alice and JWTs in the logs of every container of the project."""
    s = h.Scenario("A9b", "PAT and token leak in the logs of all compose containers",
                   "no alice-pat, alice token or JWT-shaped strings except the expected ones")
    token = ctx["alice_token"]
    listing = subprocess.run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}",
                              "--format", "{{.Names}}"], capture_output=True, text=True).stdout.split()
    needles = {name: name for name in m1.PAT_NAMES}
    needles["IdP token of alice"] = token
    needles["IdP token signature of alice"] = token.rsplit(".", 1)[-1]
    for name in sorted(listing):
        text = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        text = text.stdout + text.stderr
        hits = [label for label, needle in needles.items() if needle in text]
        s.check(f"{name}: no PAT or alice token ({len(text)} B of log)", not hits, ", ".join(hits))
        jwts = JWT_RE.findall(text)
        s.check(f"{name}: no JWT-shaped strings", not jwts, f"{len(jwts)} hits")
    return s


def main():
    try:
        preflight()
    except h.Blocked as e:
        sys.stderr.write(f"BLOCKED: {e}\n")
        return 2
    patch_e2e()
    broker = ContainerComponent("mcp-credential-broker")
    logdir = Path(tempfile.mkdtemp(prefix="m1-compose-"))
    ctx = {"logdir": logdir, "comps": {"broker": broker}, "clients": []}
    scenarios = []
    try:
        for fn in (m1.scenario_functional, m1.scenario_idp_token_direct, m1.scenario_forged_headers,
                   m1.scenario_direct_server, m1.scenario_broker_down):
            try:
                scenarios.append(fn(ctx))
            except Exception as e:
                sc = h.Scenario(fn.__name__, fn.__doc__ or fn.__name__, "")
                sc.error = f"{type(e).__name__}: {e}"
                scenarios.append(sc)
    finally:
        if not broker.alive():
            broker.start()
    for sc in scenarios:
        if sc.sid == "A15":
            sc.note = ""
    scenarios.append(m1.scenario_leak(ctx))
    scenarios.append(leak_all_containers(ctx))
    order = {"F1": 0, "A1": 1, "A2": 2, "A3": 3, "A9": 4, "A9b": 5, "A15": 6}
    scenarios.sort(key=lambda sc: order.get(sc.sid, 9))
    for sc in scenarios:
        sys.stdout.write(f"{sc.result:4} {sc.sid:4} {sc.title}\n")
        if sc.error:
            sys.stdout.write(f"       ERROR {sc.error}\n")
        for c in sc.checks:
            sys.stdout.write(f"       {'ok  ' if c.ok else 'FAIL'} {c.description}"
                             f"{'' if c.ok else ' ' + c.detail[:160]}\n")
    return 0 if all(sc.passed for sc in scenarios) else 1


if __name__ == "__main__":
    sys.exit(main())
