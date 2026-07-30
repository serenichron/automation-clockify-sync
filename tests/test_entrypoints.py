from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class CollectorEntrypointTests(unittest.TestCase):
    def test_top_level_entrypoint_delegates_to_canonical_module(self):
        command = [sys.executable, str(ROOT / "clockify_sync_collect.py"), "--help"]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Clockify sync dry-run collector", result.stdout)

    def test_only_one_collector_implementation_remains(self):
        wrapper = (ROOT / "clockify_sync_collect.py").read_text()
        self.assertIn("from scripts.clockify_sync_collect import main", wrapper)
        self.assertLess(len(wrapper.splitlines()), 20)


if __name__ == "__main__":
    unittest.main()
