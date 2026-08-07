"""Loads and validates config.yaml, resolving directories as needed."""
import os
from pathlib import Path
from urllib.parse import urlparse

import yaml

_CONFIG_CACHE = None
REQUIRED_DIR_KEYS = ("pull_folder", "index_folder", "pending_folder")

def _split_tokens(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]

def _normalize_tokens(tokens: list[str]) -> list[str]:
    normalized = []
    for token in tokens:
        t = (token or "").strip()
        if not t:
            continue
        if t.lower().startswith("set-in-env"):
            continue
        normalized.append(t)
    return normalized

def _apply_env_overrides(cfg: dict) -> dict:
    cl_cfg = cfg.setdefault("courtlistener", {})
    env_tokens = os.getenv("COURTLISTENER_API_TOKENS")
    env_token = os.getenv("COURTLISTENER_API_TOKEN")
    env_base = os.getenv("COURTLISTENER_BASE_URL")

    if env_tokens:
        cl_cfg["api_tokens"] = _normalize_tokens(_split_tokens(env_tokens))
    elif env_token:
        cl_cfg["api_tokens"] = _normalize_tokens([env_token.strip()])
    else:
        tokens = cl_cfg.get("api_tokens", [])
        if not tokens and cl_cfg.get("api_token"):
            tokens = [cl_cfg.get("api_token")]
        cl_cfg["api_tokens"] = _normalize_tokens(tokens)

    if env_base:
        cl_cfg["base_url"] = env_base.strip()

    return cfg


def _inject_defaults(cfg: dict) -> dict:
    llm_cfg = cfg.setdefault("llm", {})
    llm_cfg.setdefault("base_url", "https://api.openai.com/v1")
    llm_cfg.setdefault("api_key", "")
    llm_cfg.setdefault("model", "gpt-4o-mini")
    llm_cfg.setdefault("max_context_chars", 12000)

    prod_cfg = cfg.setdefault("production", {})
    prod_cfg.setdefault("enabled", False)
    prod_cfg.setdefault("support_email", "")
    prod_cfg.setdefault("backup_folder", "")
    prod_cfg.setdefault("audit_log_path", "logs/audit.log")
    qg_cfg = prod_cfg.setdefault("quality_gate", {})
    qg_cfg.setdefault("citation_f1_min", 0.70)
    qg_cfg.setdefault("entity_f1_min", 0.60)
    qg_cfg.setdefault("min_cases", 1)
    return cfg


def validate_config(cfg: dict, strict: bool = False) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for key in REQUIRED_DIR_KEYS:
        value = cfg.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} must be a non-empty string path.")

    cl_cfg = cfg.get("courtlistener", {}) or {}
    base_url = str(cl_cfg.get("base_url", "")).strip()
    if not base_url:
        errors.append("courtlistener.base_url is required.")
    else:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            errors.append("courtlistener.base_url must start with http:// or https://.")

    llm_cfg = cfg.get("llm", {}) or {}
    max_context_chars = llm_cfg.get("max_context_chars", 12000)
    try:
        if int(max_context_chars) < 1000:
            errors.append("llm.max_context_chars must be >= 1000.")
    except Exception:
        errors.append("llm.max_context_chars must be an integer.")

    prod_cfg = cfg.get("production", {}) or {}
    if strict:
        if not str(prod_cfg.get("support_email", "")).strip():
            errors.append("production.support_email is required in strict readiness mode.")
        if not str(prod_cfg.get("backup_folder", "")).strip():
            errors.append("production.backup_folder is required in strict readiness mode.")
    else:
        if not str(prod_cfg.get("support_email", "")).strip():
            warnings.append("production.support_email is not set.")
        if not str(prod_cfg.get("backup_folder", "")).strip():
            warnings.append("production.backup_folder is not set.")

    qg_cfg = prod_cfg.get("quality_gate", {}) or {}
    for key in ("citation_f1_min", "entity_f1_min"):
        value = qg_cfg.get(key)
        try:
            f = float(value)
            if f < 0 or f > 1:
                errors.append(f"production.quality_gate.{key} must be between 0 and 1.")
        except Exception:
            errors.append(f"production.quality_gate.{key} must be a number between 0 and 1.")
    try:
        if int(qg_cfg.get("min_cases", 1)) < 1:
            errors.append("production.quality_gate.min_cases must be >= 1.")
    except Exception:
        errors.append("production.quality_gate.min_cases must be an integer.")

    return errors, warnings


def load_config(path: str = None, strict: bool = False) -> dict:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    if path is None:
        # config.yaml lives one level above src/
        path = Path(__file__).resolve().parent.parent / "config.yaml"

    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    if not isinstance(cfg, dict):
        raise ValueError("config.yaml must define a YAML mapping/object at the top level.")

    cfg = _apply_env_overrides(cfg)
    cfg = _inject_defaults(cfg)
    errors, warnings = validate_config(cfg, strict=strict)
    if errors:
        bullets = "\n - ".join(errors)
        raise ValueError(f"Invalid config.yaml:\n - {bullets}")
    cfg["_warnings"] = warnings

    for key in REQUIRED_DIR_KEYS:
        Path(cfg[key]).mkdir(parents=True, exist_ok=True)
    backup_folder = str(cfg.get("production", {}).get("backup_folder", "")).strip()
    if backup_folder:
        Path(backup_folder).mkdir(parents=True, exist_ok=True)

    _CONFIG_CACHE = cfg
    return cfg
