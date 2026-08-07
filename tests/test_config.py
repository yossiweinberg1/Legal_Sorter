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
        os.environ.pop("LEGAL_SORTER_API_KEYS", None)
        os.environ.pop("LEGAL_SORTER_AUTH_ENABLED", None)

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

    def test_load_config_rejects_missing_required_paths(self):
        cfg_path = self._write_cfg(
            """
index_folder: "/tmp/index"
pending_folder: "/tmp/pending"
courtlistener:
  base_url: "https://example.com"
  api_tokens: []
"""
        )
        with self.assertRaises(ValueError):
            cfgmod.load_config(str(cfg_path))

    def test_load_config_injects_llm_defaults(self):
        base = Path(self.tmp.name)
        cfg_path = self._write_cfg(
            f"""
pull_folder: "{base / 'pull3'}"
index_folder: "{base / 'index3'}"
pending_folder: "{base / 'pending3'}"
courtlistener:
  base_url: "https://example.com"
  api_tokens: []
"""
        )
        cfg = cfgmod.load_config(str(cfg_path))
        self.assertEqual(cfg["llm"]["model"], "gpt-4o-mini")
        self.assertEqual(cfg["llm"]["max_context_chars"], 12000)
        self.assertEqual(cfg["production"]["quality_gate"]["citation_f1_min"], 0.70)
        self.assertEqual(cfg["production"]["quality_gate"]["entity_f1_min"], 0.60)
        self.assertIn("production.support_email is not set.", cfg.get("_warnings", []))

    def test_auth_env_override_parses_role_key_pairs(self):
        base = Path(self.tmp.name)
        cfg_path = self._write_cfg(
            f"""
pull_folder: "{base / 'pull4'}"
index_folder: "{base / 'index4'}"
pending_folder: "{base / 'pending4'}"
courtlistener:
  base_url: "https://example.com"
  api_tokens: []
"""
        )
        os.environ["LEGAL_SORTER_AUTH_ENABLED"] = "true"
        os.environ["LEGAL_SORTER_API_KEYS"] = "admin:secret-a,reader:secret-b"
        cfg = cfgmod.load_config(str(cfg_path))
        self.assertTrue(cfg["auth"]["enabled"])
        self.assertEqual(cfg["auth"]["api_keys"][0]["role"], "admin")
        self.assertEqual(cfg["auth"]["api_keys"][1]["role"], "reader")


if __name__ == "__main__":
    unittest.main()
