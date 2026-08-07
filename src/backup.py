"""Backup utilities for production readiness."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def create_backup(cfg: dict) -> Path:
    index_folder = Path(str(cfg["index_folder"]))
    backup_folder = Path(str(cfg.get("production", {}).get("backup_folder", "")).strip())
    if not backup_folder:
        raise ValueError("production.backup_folder is required for backup command.")
    backup_folder.mkdir(parents=True, exist_ok=True)

    db_path = index_folder / "legal_sorter.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = backup_folder / f"legal_sorter_backup_{ts}.zip"

    with ZipFile(out_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.write(db_path, arcname="index/legal_sorter.db")
        for sidecar in (db_path.with_suffix(".db-wal"), db_path.with_suffix(".db-shm")):
            if sidecar.exists():
                zf.write(sidecar, arcname=f"index/{sidecar.name}")
        cfg_path = Path("config.yaml")
        if cfg_path.exists():
            zf.write(cfg_path, arcname="config/config.yaml")
        ui_errors = Path("ui_extraction_errors.json")
        if ui_errors.exists():
            zf.write(ui_errors, arcname="logs/ui_extraction_errors.json")
    return out_path

