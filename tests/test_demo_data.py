import os
import tempfile
import unittest
from pathlib import Path

from src import config as cfgmod
from src.demo_data import load_demo_data


class DemoDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.old_cwd = Path.cwd()
        os.chdir(self.base)
        self.addCleanup(os.chdir, self.old_cwd)
        cfgmod._CONFIG_CACHE = {}
        (self.base / "config.yaml").write_text(
            "\n".join(
                [
                    f'pull_folder: "{self.base / "pull"}"',
                    f'index_folder: "{self.base / "index"}"',
                    f'pending_folder: "{self.base / "pending"}"',
                    "courtlistener:",
                    '  base_url: "https://example.com"',
                    "  api_tokens: []",
                ]
            ),
            encoding="utf-8",
        )

    def test_load_demo_data_ingests_cases(self):
        result = load_demo_data()
        self.assertEqual(result["loaded"], 3)
        self.assertGreaterEqual(result["total_cases"], 3)


if __name__ == "__main__":
    unittest.main()
