"""Durable audit-log helpers with basic redaction."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SENSITIVE_KEYS = {"api_key", "authorization", "token", "secret", "password"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, raw in value.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact(raw)
        return redacted
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _audit_path(cfg: dict) -> Path:
    raw = str(((cfg or {}).get("production", {}) or {}).get("audit_log_path", "logs/audit.log")).strip()
    path = Path(raw or "logs/audit.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def log_event(
    cfg: dict,
    event_type: str,
    *,
    actor: str = "system",
    role: str = "system",
    status: str = "ok",
    details: dict | None = None,
) -> dict:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "actor": actor,
        "role": role,
        "status": status,
        "details": _redact(details or {}),
    }
    with _audit_path(cfg).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
    return entry


def read_events(cfg: dict, limit: int = 100) -> list[dict]:
    path = _audit_path(cfg)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    events: list[dict] = []
    for line in reversed(lines[-max(1, int(limit)):]):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events
