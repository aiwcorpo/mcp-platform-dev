#!/usr/bin/env python3
"""Manifest of a stand-in on stdout: the one path used by scripts/bulk.sh (main stack) and scripts/e2e-prod.sh (profile e2e-prod).
  stub-manifest.py jira|confluence [dev|production|none]
jira: the xh toolset as mcp-jira. confluence: the bulk toolset on top of the build artifact of the real mcp-confluence (the credential,
version, resource types and the four real tools come from it). Every hash is checked against the credential of the manifest before printing.
"""
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "mocks" / "mcp"))
import mcp_stub  # noqa: E402

which = sys.argv[1] if len(sys.argv) > 1 else ""
environment = sys.argv[2] if len(sys.argv) > 2 else "none"
if which == "jira":
    os.environ.update({"MCP_STUB_SERVER_ID": "mcp-jira", "MCP_STUB_SYSTEM": "jira", "MCP_STUB_TOOLSET": "xh",
                       "MCP_STUB_INTERNAL_URL": "https://mcp-jira.internal:8443/mcp"})
elif which == "confluence":
    os.environ["MCP_STUB_BASE_MANIFEST"] = str(root / ".." / "mcp-server-confluence" / "mcp-app" / "target" / "mcp-manifest.json")
else:
    sys.exit("usage: stub-manifest.py jira|confluence [dev|production|none]")
os.environ.update({"MCP_STUB_CREDENTIAL_REQUIRED": "true", "MCP_STUB_CREDENTIAL_TYPES": "user-secret", "MCP_STUB_TOOL_COUNT": "250"})
settings = mcp_stub.Settings.from_environment()
manifest = mcp_stub.build_manifest(settings, environment)
bad = [t["name"] for t in manifest["tools"]
       if t["definitionHash"] != mcp_stub.definition_hash(t["definition"], t, manifest["system"], manifest["credential"])]
if bad:
    sys.exit(f"stub-manifest: hash does not match the credential of the manifest for {len(bad)} tools, e.g. {bad[:3]}")
sys.exit(mcp_stub.main(["manifest", "--environment", environment]))
