"""Local readiness checks for running Legal Sorter."""

from __future__ import annotations

import importlib
import os
import sqlite3
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


def _check_config_and_db() -> tuple[bool, str]:
    cfgmod._CONFIG_CACHE = None
    cfg = cfgmod.load_config()
    required = ("pull_folder", "index_folder", "pending_folder")
    missing_keys = [k for k in required if not cfg.get(k)]
    if missing_keys:
        return False, f"Missing config keys: {', '.join(missing_keys)}"

    for key in required:
        path = Path(cfg[key])
        if not path.exists():
            return False, f"Configured folder does not exist: {path}"
        if not path.is_dir():
            return False, f"Configured path is not a folder: {path}"

    db_path = Path(cfg["index_folder"]) / "legal_sorter.db"
    DB(str(db_path))
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("SELECT 1 FROM documents LIMIT 1")
    except Exception as exc:
        return False, f"Database check failed: {exc}"

    token_ok = bool(os.getenv("COURTLISTENER_API_TOKEN") or os.getenv("COURTLISTENER_API_TOKENS"))
    llm_ok = bool(os.getenv("LLM_API_KEY"))
    msg = "Config and database are valid."
    if not token_ok:
        msg += " COURTLISTENER_API_TOKEN(S) not set."
    if not llm_ok:
        msg += " LLM_API_KEY not set (only needed for cloud LLM mode)."
    return True, msg


def run_health_check() -> int:
    print("== Legal Sorter Health Check ==")
    checks = [
        ("Python", _check_python),
        ("Dependencies", _check_imports),
        ("Config + Database", _check_config_and_db),
    ]

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
