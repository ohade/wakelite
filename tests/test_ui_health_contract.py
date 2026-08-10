import shutil
import subprocess
import unittest
from pathlib import Path


class UIHealthContractTests(unittest.TestCase):
    def test_health_metrics_execute_against_dom_contract(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required to test the dashboard JavaScript")
        script = Path(__file__).with_name("ui_health_contract.test.js")
        result = subprocess.run(
            [node, str(script)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"dashboard JavaScript contract failed:\n{result.stdout}{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
