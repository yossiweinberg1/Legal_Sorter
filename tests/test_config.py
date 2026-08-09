import os
import tempfile
import unittest
from pathlib import Path

from src import config as cfgmod


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._old_cache = cfgmod._CONFIG_CACHE
        cfgmod._CONFIG_CACHE = {}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        cfgmod._CONFIG_CACHE = self._old_cache
        os.environ.pop("COURTLISTENER_API_TOKEN", None)
        os.environ.pop("COURTLISTENER_API_TOKENS", None)
        os.environ.pop("COURTLISTENER_BASE_URL", None)
        os.environ.pop("LEGAL_SORTER_API_KEYS", None)
        os.environ.pop("LEGAL_SORTER_AUTH_ENABLED", None)
        os.environ.pop("LEGAL_SORTER_PULL_FOLDER", None)
        os.environ.pop("LEGAL_SORTER_INDEX_FOLDER", None)
        os.environ.pop("LEGAL_SORTER_PENDING_FOLDER", None)
        os.environ.pop("LEGAL_SORTER_LLM_BASE_URL", None)
        os.environ.pop("LEGAL_SORTER_LLM_MODEL", None)
        os.environ.pop("LEGAL_SORTER_LLM_FAST_MODEL", None)
        os.environ.pop("LEGAL_SORTER_LLM_ACCURATE_MODEL", None)
        os.environ.pop("LEGAL_SORTER_BACKUP_FOLDER", None)
        os.environ.pop("LEGAL_SORTER_AUDIT_LOG_PATH", None)
        os.environ.pop("LEGAL_SORTER_QUARANTINE_FOLDER", None)
        os.environ.pop("LEGAL_SORTER_SUPPORT_EMAIL", None)
        os.environ.pop("LEGAL_SORTER_TELEMETRY_ENABLED", None)

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
        self.assertFalse(cfg["production"]["telemetry_enabled"])
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

    def test_dotenv_is_loaded_from_config_directory(self):
        base = Path(self.tmp.name)
        cfg_path = self._write_cfg(
            f"""
pull_folder: "{base / 'pull5'}"
index_folder: "{base / 'index5'}"
pending_folder: "{base / 'pending5'}"
courtlistener:
  base_url: "https://example.com"
  api_tokens: []
"""
        )
        (base / ".env").write_text(
            'COURTLISTENER_API_TOKEN=dotenv-token\nLEGAL_SORTER_SUPPORT_EMAIL="owner@example.com"\n',
            encoding="utf-8",
        )
        cfg = cfgmod.load_config(str(cfg_path))
        self.assertEqual(cfg["courtlistener"]["api_tokens"], ["dotenv-token"])
        self.assertEqual(cfg["production"]["support_email"], "owner@example.com")

    def test_directory_env_overrides_apply(self):
        base = Path(self.tmp.name)
        cfg_path = self._write_cfg(
            f"""
pull_folder: "{base / 'pull6'}"
index_folder: "{base / 'index6'}"
pending_folder: "{base / 'pending6'}"
courtlistener:
  base_url: "https://example.com"
  api_tokens: []
"""
        )
        os.environ["LEGAL_SORTER_PULL_FOLDER"] = str(base / "env-pull")
        os.environ["LEGAL_SORTER_INDEX_FOLDER"] = str(base / "env-index")
        os.environ["LEGAL_SORTER_PENDING_FOLDER"] = str(base / "env-pending")
        cfg = cfgmod.load_config(str(cfg_path))
        self.assertEqual(cfg["pull_folder"], str(base / "env-pull"))
        self.assertEqual(cfg["index_folder"], str(base / "env-index"))
        self.assertEqual(cfg["pending_folder"], str(base / "env-pending"))
        self.assertTrue((base / "env-pull").exists())

    def test_dotenv_strips_inline_comments_from_unquoted_values(self):
        base = Path(self.tmp.name)
        cfg_path = self._write_cfg(
            f"""
pull_folder: "{base / 'pull7'}"
index_folder: "{base / 'index7'}"
pending_folder: "{base / 'pending7'}"
courtlistener:
  base_url: "https://example.com"
  api_tokens: []
"""
        )
        (base / ".env").write_text(
            "COURTLISTENER_API_TOKEN=dotenv-token # keep comment out of value\n",
            encoding="utf-8",
        )
        cfg = cfgmod.load_config(str(cfg_path))
        self.assertEqual(cfg["courtlistener"]["api_tokens"], ["dotenv-token"])

    def test_llm_env_overrides_apply(self):
        base = Path(self.tmp.name)
        cfg_path = self._write_cfg(
            f"""
pull_folder: "{base / 'pull8'}"
index_folder: "{base / 'index8'}"
pending_folder: "{base / 'pending8'}"
courtlistener:
  base_url: "https://example.com"
  api_tokens: []
"""
        )
        os.environ["LEGAL_SORTER_LLM_BASE_URL"] = "http://localhost:11434/v1"
        os.environ["LEGAL_SORTER_LLM_MODEL"] = "llama3"
        os.environ["LEGAL_SORTER_LLM_FAST_MODEL"] = "llama3"
        os.environ["LEGAL_SORTER_LLM_ACCURATE_MODEL"] = "llama3"
        cfg = cfgmod.load_config(str(cfg_path))
        self.assertEqual(cfg["llm"]["base_url"], "http://localhost:11434/v1")
        self.assertEqual(cfg["llm"]["model"], "llama3")


if __name__ == "__main__":
    unittest.main()
