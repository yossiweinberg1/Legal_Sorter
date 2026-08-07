"""Loads and validates config.yaml, resolving directories as needed."""
import os
from pathlib import Path
from urllib.parse import urlparse

import yaml

_CONFIG_CACHE = {}
REQUIRED_DIR_KEYS = ("pull_folder", "index_folder", "pending_folder")
ALLOWED_ROLES = {"reader", "operator", "admin"}

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


def _parse_bool(raw: str | None) -> bool | None:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def _parse_api_keys(raw: str) -> list[dict]:
    parsed: list[dict] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(
                "LEGAL_SORTER_API_KEYS entries must be in role:key format."
            )
        role, key = item.split(":", 1)
        role = role.strip().lower()
        key = key.strip()
        if role not in ALLOWED_ROLES:
            raise ValueError(f"Unsupported auth role: {role}")
        if not key:
            raise ValueError("Auth API keys must not be empty.")
        parsed.append({"role": role, "key": key})
    return parsed


def _strip_wrapping_quotes(value: str) -> str:
    raw = (value or "").strip()
    in_single = False
    in_double = False
    for idx, char in enumerate(raw):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double and (idx == 0 or raw[idx - 1].isspace()):
            raw = raw[:idx].rstrip()
            break
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        return raw[1:-1]
    return raw


def _load_dotenv(path: str | Path | None) -> None:
    if not path:
        return
    env_path = Path(path)
    if not env_path.exists() or not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower().startswith("export "):
            stripped = stripped[7:].strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _strip_wrapping_quotes(value)

def _apply_env_overrides(cfg: dict) -> dict:
    for key in REQUIRED_DIR_KEYS:
        env_key = f"LEGAL_SORTER_{key.upper()}"
        env_value = os.getenv(env_key)
        if env_value:
            cfg[key] = env_value.strip()

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

    auth_cfg = cfg.setdefault("auth", {})
    auth_enabled = _parse_bool(os.getenv("LEGAL_SORTER_AUTH_ENABLED"))
    if auth_enabled is not None:
        auth_cfg["enabled"] = auth_enabled
    auth_keys = os.getenv("LEGAL_SORTER_API_KEYS")
    if auth_keys:
        auth_cfg["api_keys"] = _parse_api_keys(auth_keys)

    prod_cfg = cfg.setdefault("production", {})
    backup_folder = os.getenv("LEGAL_SORTER_BACKUP_FOLDER")
    if backup_folder:
        prod_cfg["backup_folder"] = backup_folder.strip()
    audit_log_path = os.getenv("LEGAL_SORTER_AUDIT_LOG_PATH")
    if audit_log_path:
        prod_cfg["audit_log_path"] = audit_log_path.strip()
    quarantine_folder = os.getenv("LEGAL_SORTER_QUARANTINE_FOLDER")
    if quarantine_folder:
        prod_cfg["quarantine_folder"] = quarantine_folder.strip()
    support_email = os.getenv("LEGAL_SORTER_SUPPORT_EMAIL")
    if support_email:
        prod_cfg["support_email"] = support_email.strip()
    telemetry_enabled = _parse_bool(os.getenv("LEGAL_SORTER_TELEMETRY_ENABLED"))
    if telemetry_enabled is not None:
        prod_cfg["telemetry_enabled"] = telemetry_enabled

    return cfg


def _inject_defaults(cfg: dict) -> dict:
    llm_cfg = cfg.setdefault("llm", {})
    llm_cfg.setdefault("base_url", "https://api.openai.com/v1")
    llm_cfg.setdefault("api_key", "")
    llm_cfg.setdefault("model", "gpt-4o-mini")
    llm_cfg.setdefault("fast_model", llm_cfg.get("model", "gpt-4o-mini"))
    llm_cfg.setdefault("accurate_model", llm_cfg.get("model", "gpt-4o-mini"))
    llm_cfg.setdefault("max_context_chars", 12000)
    llm_cfg.setdefault("require_citations", True)
    llm_cfg.setdefault("min_sources", 1)
    llm_cfg.setdefault("timeout_seconds", 60)

    prod_cfg = cfg.setdefault("production", {})
    prod_cfg.setdefault("enabled", False)
    prod_cfg.setdefault("support_email", "")
    prod_cfg.setdefault("backup_folder", "")
    prod_cfg.setdefault("audit_log_path", "logs/audit.log")
    prod_cfg.setdefault("quarantine_folder", "quarantine")
    prod_cfg.setdefault("retention_days", 365)
    prod_cfg.setdefault("telemetry_enabled", False)
    qg_cfg = prod_cfg.setdefault("quality_gate", {})
    qg_cfg.setdefault("citation_f1_min", 0.70)
    qg_cfg.setdefault("entity_f1_min", 0.60)
    qg_cfg.setdefault("min_cases", 1)
    qg_cfg.setdefault("baseline_file", "tests/fixtures/gold_baseline.json")

    auth_cfg = cfg.setdefault("auth", {})
    auth_cfg.setdefault("enabled", False)
    auth_cfg.setdefault("api_keys", [])
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
    for key in ("min_sources", "timeout_seconds"):
        value = llm_cfg.get(key, 1)
        try:
            if int(value) < 1:
                errors.append(f"llm.{key} must be >= 1.")
        except Exception:
            errors.append(f"llm.{key} must be an integer.")

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
    for key in ("audit_log_path", "quarantine_folder"):
        if not str(prod_cfg.get(key, "")).strip():
            errors.append(f"production.{key} must be a non-empty string path.")
    try:
        if int(prod_cfg.get("retention_days", 365)) < 1:
            errors.append("production.retention_days must be >= 1.")
    except Exception:
        errors.append("production.retention_days must be an integer.")

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

    auth_cfg = cfg.get("auth", {}) or {}
    api_keys = auth_cfg.get("api_keys", [])
    if not isinstance(api_keys, list):
        errors.append("auth.api_keys must be a list.")
    else:
        seen_keys = set()
        for entry in api_keys:
            if not isinstance(entry, dict):
                errors.append("auth.api_keys entries must be mappings with role/key.")
                continue
            role = str(entry.get("role", "")).strip().lower()
            key = str(entry.get("key", "")).strip()
            if role not in ALLOWED_ROLES:
                errors.append(f"auth.api_keys role must be one of: {', '.join(sorted(ALLOWED_ROLES))}.")
            if not key:
                errors.append("auth.api_keys key must be non-empty.")
            if key:
                if key in seen_keys:
                    errors.append("auth.api_keys must not contain duplicate keys.")
                seen_keys.add(key)
    if bool(auth_cfg.get("enabled")) and not api_keys:
        errors.append("auth.api_keys is required when auth.enabled is true.")
    if strict and not bool(auth_cfg.get("enabled")):
        errors.append("auth.enabled must be true in strict readiness mode.")

    return errors, warnings


def load_config(path: str = None, strict: bool = False) -> dict:
    global _CONFIG_CACHE
    if not isinstance(_CONFIG_CACHE, dict):
        _CONFIG_CACHE = {}
    if path is None:
        # config.yaml lives one level above src/
        path = Path(__file__).resolve().parent.parent / "config.yaml"
    config_path = Path(path)
    _load_dotenv(config_path.parent / ".env")
    cache_key = (str(config_path), bool(strict))
    if cache_key in _CONFIG_CACHE:
        return _CONFIG_CACHE[cache_key]

    with open(config_path, "r", encoding="utf-8") as f:
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
    for key in ("audit_log_path", "quarantine_folder"):
        raw = str(cfg.get("production", {}).get(key, "")).strip()
        if raw:
            prod_path = Path(raw)
            if key == "audit_log_path":
                prod_path.parent.mkdir(parents=True, exist_ok=True)
            else:
                prod_path.mkdir(parents=True, exist_ok=True)

    _CONFIG_CACHE[cache_key] = cfg
    return cfg
