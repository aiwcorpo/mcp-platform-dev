"""The manifest of a server is built to <repo>/mcp-app/target/mcp-manifest.json; every script that looks for it must agree."""
import os
import re
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
        import m4
        os.environ.pop("MANIFEST_DIR", None)
        self.assertEqual(m4.manifest_path("mcp-jira"), ROOT.parent / "mcp-server-jira" / "mcp-app" / "target" / "mcp-manifest.json")
        os.environ["MANIFEST_DIR"] = "/x"
        try:
            self.assertEqual(m4.manifest_path("mcp-jira"), Path("/x/mcp-jira.json"))
        finally:
            del os.environ["MANIFEST_DIR"]


if __name__ == "__main__":
    unittest.main()
