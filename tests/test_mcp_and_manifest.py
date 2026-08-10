import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wakelite.mcp_server import ServiceUnavailable, TOOLS, call_tool


class MCPAndManifestTests(unittest.TestCase):
    def test_mcp_tools_include_run_abort(self):
        names = [tool["name"] for tool in TOOLS]
        self.assertIn("wakelite.v1.run.abort", names)

    def test_mcp_fail_fast_when_runner_down(self):
        with patch(
            "wakelite.mcp_server.request.urlopen",
            side_effect=ConnectionRefusedError("runner unavailable"),
        ):
            with self.assertRaises(ServiceUnavailable):
                call_tool("wakelite.v1.health.get", {})

    def test_manifest_and_registration_files(self):
        from wakelite import mcp_manifest as mm

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            mm.CLAUDE_MCP_CONFIG = base / "mcp.json"
            mm.CODEX_CONFIG = base / "config.toml"
            mm.MCP_MANIFEST_FILE = base / "manifest.json"

            manifest = mm.generate_manifest("/tmp/wakelite-mcp")
            self.assertEqual(manifest["name"], "wakelite")
            self.assertTrue(mm.MCP_MANIFEST_FILE.exists())

            result = mm.install_global_configs("/tmp/wakelite-mcp", ["claude", "codex"])
            self.assertTrue(result["claude"])
            self.assertTrue(result["codex"])

            claude = json.loads(mm.CLAUDE_MCP_CONFIG.read_text(encoding="utf-8"))
            self.assertIn("wakelite", claude["mcpServers"])

            codex = mm.CODEX_CONFIG.read_text(encoding="utf-8")
            self.assertIn("[mcp_servers.wakelite]", codex)


if __name__ == "__main__":
    unittest.main()
