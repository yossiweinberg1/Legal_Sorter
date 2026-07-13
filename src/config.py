"""Loads config.yaml and resolves paths, creating folders if needed."""
import os
import yaml
from pathlib import Path

_CONFIG_CACHE = None


def load_config(path: str = None) -> dict:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    if path is None:
        # config.yaml lives one level above src/
        path = Path(__file__).resolve().parent.parent / "config.yaml"

    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    for key in ("pull_folder", "index_folder", "pending_folder"):
        Path(cfg[key]).mkdir(parents=True, exist_ok=True)

    _CONFIG_CACHE = cfg
    return cfg
