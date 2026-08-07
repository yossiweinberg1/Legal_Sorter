"""Local readiness checks for running Legal Sorter."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from . import config as cfgmod
from .database import DB


REQUIRED_IMPORTS = [
    "yaml",
    "requests",
    "pypdf",
    "fitz",
    "docx",
    "bs4",
    "sklearn",
    "torch",
    "tokenizers",
    "watchdog",
    "fastapi",
    "uvicorn",
]


def _check_python() -> tuple[bool, str]:
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 10):
        return False, f"Python {major}.{minor} is unsupported. Use Python 3.10+."
    if (major, minor) < (3, 12):
        return True, f"Python {major}.{minor} detected (recommended: 3.12+)."
    return True, f"Python {major}.{minor} detected."


def _check_imports() -> tuple[bool, str]:
    missing = []
    for mod in REQUIRED_IMPORTS:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(mod)
    if missing:
        return False, f"Missing imports: {', '.join(missing)}"
    return True, "All required imports resolved."


def _check_config_and_db(strict: bool = False) -> tuple[bool, str]:
    cfgmod._CONFIG_CACHE = {}
    try:
        cfg = cfgmod.load_config(strict=strict)
    except Exception as exc:
        return False, f"Config validation failed: {exc}"
    required = cfgmod.REQUIRED_DIR_KEYS

    for key in required:
        path = Path(cfg[key])
        if not path.exists():
            return False, f"Configured folder does not exist: {path}"
        if not path.is_dir():
            return False, f"Configured path is not a folder: {path}"

    db_path = Path(cfg["index_folder"]) / "legal_sorter.db"
    db = None
    try:
        db = DB(str(db_path))
        db.conn.execute("SELECT 1 FROM documents LIMIT 1")
    except Exception as exc:
        return False, f"Database check failed: {exc}"
    finally:
        if db is not None:
            try:
                db.conn.close()
            except Exception:
                pass

    token_ok = bool(os.getenv("COURTLISTENER_API_TOKEN") or os.getenv("COURTLISTENER_API_TOKENS"))
    llm_ok = bool(os.getenv("LLM_API_KEY"))
    msg = "Config and database are valid."
    if not token_ok:
        msg += " COURTLISTENER_API_TOKEN(S) not set."
    if not llm_ok:
        msg += " LLM_API_KEY not set (only needed for cloud LLM mode)."
    warnings = cfg.get("_warnings", [])
    if warnings:
        msg += " Warnings: " + "; ".join(warnings)
    return True, msg


def _check_production_baseline() -> tuple[bool, str]:
    cfgmod._CONFIG_CACHE = {}
    try:
        cfg = cfgmod.load_config(strict=True)
    except Exception as exc:
        return False, f"Strict production validation failed: {exc}"

    prod = cfg.get("production", {})
    audit_log_path = Path(str(prod.get("audit_log_path", "logs/audit.log")))
    try:
        audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, f"Cannot create audit log directory: {exc}"

    backup_folder = Path(str(prod.get("backup_folder", "")))
    if not backup_folder.exists() or not backup_folder.is_dir():
        return False, f"Backup folder is invalid: {backup_folder}"
    if not os.access(backup_folder, os.W_OK):
        return False, f"Backup folder is not writable: {backup_folder}"
    quarantine_folder = Path(str(prod.get("quarantine_folder", "")))
    if not quarantine_folder.exists() or not quarantine_folder.is_dir():
        return False, f"Quarantine folder is invalid: {quarantine_folder}"
    if not os.access(quarantine_folder, os.W_OK):
        return False, f"Quarantine folder is not writable: {quarantine_folder}"

    enabled = bool(prod.get("enabled", False))
    status = "enabled" if enabled else "configured (disabled)"
    return True, f"Production baseline is {status}."


def run_health_check(strict: bool = False) -> int:
    mode = "READINESS" if strict else "HEALTH"
    print(f"== Legal Sorter {mode} Check ==")
    checks = [
        ("Python", _check_python),
        ("Dependencies", _check_imports),
        ("Config + Database", lambda: _check_config_and_db(strict=strict)),
    ]
    if strict:
        checks.append(("Production Baseline", _check_production_baseline))

    has_failure = False
    for label, fn in checks:
        ok, detail = fn()
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {label}: {detail}")
        if not ok:
            has_failure = True

    if has_failure:
        print("Health check failed.")
        return 1

    print("Health check passed.")
    return 0
