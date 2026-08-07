import os
import tempfile
import unittest
from pathlib import Path

from src import config as cfgmod


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._old_cache = cfgmod._CONFIG_CACHE
        cfgmod._CONFIG_CACHE = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        cfgmod._CONFIG_CACHE = self._old_cache
        os.environ.pop("COURTLISTENER_API_TOKEN", None)
        os.environ.pop("COURTLISTENER_API_TOKENS", None)
        os.environ.pop("COURTLISTENER_BASE_URL", None)

    def _write_cfg(self, body: str) -> Path:
        path = Path(self.tmp.name) / "config.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_load_config_creates_directories(self):
        base = Path(self.tmp.name)
        cfg_path = self._write_cfg(
            f"""
pull_folder: "{base / 'pull'}"
index_folder: "{base / 'index'}"
pending_folder: "{base / 'pending'}"
courtlistener:
  base_url: "https://example.com"
  api_tokens: []
"""
        )
        cfg = cfgmod.load_config(str(cfg_path))
        self.assertTrue(Path(cfg["pull_folder"]).exists())
        self.assertTrue(Path(cfg["index_folder"]).exists())
        self.assertTrue(Path(cfg["pending_folder"]).exists())

    def test_env_tokens_override_config_tokens(self):
        base = Path(self.tmp.name)
        cfg_path = self._write_cfg(
            f"""
pull_folder: "{base / 'pull2'}"
index_folder: "{base / 'index2'}"
pending_folder: "{base / 'pending2'}"
courtlistener:
  base_url: "https://example.com"
  api_tokens: ["from_config"]
"""
        )
        os.environ["COURTLISTENER_API_TOKENS"] = "token_a, token_b"
        cfg = cfgmod.load_config(str(cfg_path))
        self.assertEqual(cfg["courtlistener"]["api_tokens"], ["token_a", "token_b"])


if __name__ == "__main__":
    unittest.main()
