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


def preflight():
    out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
    running = set(out.stdout.split())
    need = [f"{PROJECT}-{s}-1" for s in ("keycloak", "redis", "openbao", "postgres", "jira-mock",
                                          "mcp-gateway", "mcp-credential-broker", "mcp-jira")]
    missing = [n for n in need if n not in running]
    if missing:
        raise h.Blocked("stack is not up (make up-full): " + ", ".join(missing))
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
    s = h.Scenario("A9b", "Wyciek PAT i tokenu w logach wszystkich kontenerow compose",
                   "zero alice-pat, tokenu alice i ciagow JWT poza oczekiwanymi")
    token = ctx["alice_token"]
    listing = subprocess.run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}",
                              "--format", "{{.Names}}"], capture_output=True, text=True).stdout.split()
    needles = {name: name for name in m1.PAT_NAMES}
    needles["token IdP alice"] = token
    needles["podpis tokenu IdP alice"] = token.rsplit(".", 1)[-1]
    for name in sorted(listing):
        text = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        text = text.stdout + text.stderr
        hits = [label for label, needle in needles.items() if needle in text]
        s.check(f"{name}: brak PAT i tokenu alice ({len(text)} B logu)", not hits, ", ".join(hits))
        jwts = JWT_RE.findall(text)
        s.check(f"{name}: brak ciagow w formacie JWT", not jwts, f"{len(jwts)} trafien")
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
