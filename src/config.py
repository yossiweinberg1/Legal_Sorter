"""Loads config.yaml and resolves paths, creating folders if needed."""
import os
import yaml
from pathlib import Path

_CONFIG_CACHE = None

def _split_tokens(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]

def _apply_env_overrides(cfg: dict) -> dict:
    cl_cfg = cfg.setdefault("courtlistener", {})
    env_tokens = os.getenv("COURTLISTENER_API_TOKENS")
    env_token = os.getenv("COURTLISTENER_API_TOKEN")
    env_base = os.getenv("COURTLISTENER_BASE_URL")

    if env_tokens:
        cl_cfg["api_tokens"] = _split_tokens(env_tokens)
    elif env_token:
        cl_cfg["api_tokens"] = [env_token.strip()]

    if env_base:
        cl_cfg["base_url"] = env_base.strip()

    return cfg


def load_config(path: str = None) -> dict:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    if path is None:
        # config.yaml lives one level above src/
        path = Path(__file__).resolve().parent.parent / "config.yaml"

    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    cfg = _apply_env_overrides(cfg or {})

    for key in ("pull_folder", "index_folder", "pending_folder"):
        Path(cfg[key]).mkdir(parents=True, exist_ok=True)

    _CONFIG_CACHE = cfg
    return cfg
