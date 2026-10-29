"""The manifest of a server is built to <repo>/mcp-app/target/mcp-manifest.json; every script that looks for it must agree."""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


class ManifestPathTest(unittest.TestCase):
    def test_scripts_use_the_app_module_path(self):
        for name in ("registry-register.sh", "apply-m4-switch.sh"):
            text = (ROOT / "scripts" / name).read_text()
            paths = re.findall(r"mcp-server-\S*?/(\S*mcp-manifest\.json)", text)
            self.assertTrue(paths, name)
            self.assertEqual({"mcp-app/target/mcp-manifest.json"}, set(paths), name)

    def test_m4_default_and_manifest_dir_override(self):
        # A subprocess: importing m4 here would load mcpdev with the real configuration before the fake-server tests set theirs.
        code = ("import sys; sys.path.insert(0, %r); import m4; print(m4.manifest_path('mcp-jira'))" % str(ROOT / "scripts"))
        for override, expected in ((None, ROOT.parent / "mcp-server-jira" / "mcp-app" / "target" / "mcp-manifest.json"),
                                   ("/x", Path("/x/mcp-jira.json"))):
            env = {k: v for k, v in os.environ.items() if k != "MANIFEST_DIR"}
            if override:
                env["MANIFEST_DIR"] = override
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env).stdout.strip()
            self.assertEqual(Path(out), expected)


if __name__ == "__main__":
    unittest.main()
