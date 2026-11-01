#!/usr/bin/env python3
"""Static checks of the rendered compose configuration of each layer (M3, +M4, +M5), without the contract stubs of profile
infra (they share network aliases with the real components on purpose). Changes nothing, starts nothing.

Needs .secrets/dev.env (make secrets): compose interpolates it. Exit code 0 when nothing FAILed.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = []


def record(ok, name, detail=""):
    RESULTS.append((ok, name, detail))


def render(m4, m5):
    env = {**os.environ, "M4_ACTIVE": "1" if m4 else "0", "M5_ACTIVE": "1" if m5 else "0"}
    out = subprocess.run([str(ROOT / "scripts" / "compose.sh"), "--profile", "full", "--profile", "m2",
                          "--profile", "ci", "config", "--format", "json"], capture_output=True, text=True, env=env)
    if out.returncode != 0:
        record(False, f"compose config m4={m4} m5={m5}", out.stderr.strip()[-300:])
        return None
    record(True, f"compose config m4={m4} m5={m5} renders")
    return json.loads(out.stdout)


def published(config):
    ports = {}
    for name, service in config["services"].items():
        for port in service.get("ports", []):
            ports.setdefault((port.get("host_ip"), int(port["published"])), []).append(name)
    return ports


def aliases(config):
    owners = {}
    for name, service in config["services"].items():
        for network in (service.get("networks") or {}).values():
            for alias in (network or {}).get("aliases") or []:
                owners.setdefault(alias, set()).add(name)
    return owners


def check_ports(config, label):
    ports = published(config)
    record(all(len(v) == 1 for v in ports.values()), f"{label}: no host port published twice",
           str({k: v for k, v in ports.items() if len(v) > 1}))
    record(all(host == "127.0.0.1" for host, _ in ports), f"{label}: every published port binds 127.0.0.1")


def check_m5(base, m5):
    new_ports = {port for host, port in published(m5) if (host, port) not in published(base)}
    new_ports -= {18000, 18445, 18100}
    record(new_ports and all(18747 <= p <= 18759 for p in new_ports), "m5: new host ports are in 18747-18759", str(sorted(new_ports)))
    # The M4 stack publishes 18000/18445/18100 on the components; in M5 they move to the balancers.
    ports = published(m5)
    record(ports.get(("127.0.0.1", 18000)) == ["gateway-lb"] and ports.get(("127.0.0.1", 18445)) == ["gateway-lb"]
           and ports.get(("127.0.0.1", 18100)) == ["broker-lb"], "m5: GATEWAY_PORT, GATEWAY_INTERNAL_PORT and BROKER_PORT belong to the balancers")
    owners = aliases(m5)
    record(owners.get("mcp-gateway.internal") == {"gateway-lb"}, "m5: the gateway TLS name resolves to the balancer only", str(owners.get("mcp-gateway.internal")))
    record(owners.get("mcp-credential-broker.internal") == {"broker-lb"}, "m5: the broker TLS name resolves to the balancer only", str(owners.get("mcp-credential-broker.internal")))
    record(owners.get("gateway-backends.internal") == {"mcp-gateway", "mcp-gateway-2"}, "m5: gateway-backends.internal is on both gateway instances")
    record(owners.get("broker-backends.internal") == {"mcp-credential-broker", "mcp-credential-broker-2"}, "m5: broker-backends.internal is on both brokers")
    services = m5["services"]
    for first, second in (("mcp-gateway", "mcp-gateway-2"), ("mcp-credential-broker", "mcp-credential-broker-2")):
        a, b = services[first], services[second]
        record(a["environment"] == b["environment"], f"m5: {first} and {second} have the same environment",
               str(sorted(set(a["environment"].items()) ^ set(b["environment"].items()))[:4]))
        record(a["image"] == b["image"], f"m5: {first} and {second} run the same image")
    state = [v["source"] for v in services["mcp-gateway"]["volumes"] if v["target"] == "/app/state"]
    state2 = [v["source"] for v in services["mcp-gateway-2"]["volumes"] if v["target"] == "/app/state"]
    record(state and state2 and state != state2, "m5: the gateway instances have separate policy state volumes", f"{state} {state2}")
    cursor = [services[n]["environment"].get("MCP_LIST_CURSOR_KEY", "") for n in ("mcp-gateway", "mcp-gateway-2")]
    record(cursor[0] == cursor[1] and len(cursor[0]) >= 32, "m5: both gateway instances have the same list cursor key of at least 32 characters")
    record(services["mcp-gateway"]["environment"].get("MCP_TOOLS_LIST_TIMEOUT") == "3000", "m5: MCP_TOOLS_LIST_TIMEOUT is set on the gateway")
    record({"MCP_INTERNAL_JWT_KEYS_FILE", "MCP_INTERNAL_JWT_ACTIVE_KID"} <= set(services["mcp-gateway"]["environment"]),
           "m5: the gateway instances share the keys of the internal JWT")
    for name in ("mcp-gateway", "mcp-gateway-2", "mcp-jira", "mcp-confluence", "mcp-gitlab", "mcp-servicedesk", "mcp-opensearch"):
        service = services[name]
        record(service.get("mem_limit") and "MaxRAMPercentage" in service["environment"].get("JAVA_TOOL_OPTIONS", "")
               and "ExitOnOutOfMemoryError" in service["environment"]["JAVA_TOOL_OPTIONS"], f"m5: {name} has a memory limit and a heap sized to it")
    registry = services["registry"]
    record(registry.get("user") == "65534:65534" and registry.get("read_only") and registry["environment"].get("REGISTRY_STORAGE_DELETE_ENABLED") == "false",
           "m5: the registry runs as nobody, read-only, without deletion")
    record("ci" in m5["networks"] and all(set(services[s]["networks"]) == {"ci"} for s in ("packages", "registry")),
           "m5: package server and registry are only in the ci network")
    record(not services["packages"].get("environment") and all(k.startswith("REGISTRY_") for k in services["registry"]["environment"]),
           "m5: artifact services get no stack secrets")
    mounts = {v["target"]: v["source"] for v in services["proxy"]["volumes"]}
    record(mounts["/etc/nginx/routes.conf"].endswith("proxy/m5/routes.conf") and mounts["/etc/nginx/to-gateway.conf"].endswith("proxy/m5/to-gateway.conf"),
           "m5: the proxy mounts the M5 routes")
    record(services["redis"]["image"] == base["services"]["redis"]["image"] and "redis-2" not in services, "m5: one shared Redis")


def main():
    base = render(False, False)
    m4 = render(True, False)
    m5 = render(True, True)
    for label, config in (("m3", base), ("m4", m4), ("m5", m5)):
        if config:
            check_ports(config, label)
    if m4 and m5:
        check_m5(m4, m5)
    for ok, name, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL':4} {name}" + (f"  [{detail}]" if not ok and detail else ""))
    failed = sum(1 for ok, _, _ in RESULTS if not ok)
    print(f"check-compose: {len(RESULTS) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
