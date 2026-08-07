"""Backup and restore utilities for production readiness."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _backup_inputs(cfg: dict) -> list[tuple[Path, str]]:
    index_folder = Path(str(cfg["index_folder"]))
    db_path = index_folder / "legal_sorter.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    inputs: list[tuple[Path, str]] = [(db_path, "index/legal_sorter.db")]
    for sidecar in (db_path.with_suffix(".db-wal"), db_path.with_suffix(".db-shm")):
        if sidecar.exists():
            inputs.append((sidecar, f"index/{sidecar.name}"))
    for path, arcname in [
        (Path("config.yaml"), "config/config.yaml"),
        (Path("ui_extraction_errors.json"), "logs/ui_extraction_errors.json"),
    ]:
        if path.exists():
            inputs.append((path, arcname))
    return inputs


def create_backup(cfg: dict) -> Path:
    backup_folder = Path(str(cfg.get("production", {}).get("backup_folder", "")).strip())
    if not backup_folder:
        raise ValueError("production.backup_folder is required for backup command.")
    backup_folder.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = backup_folder / f"legal_sorter_backup_{ts}.zip"
    inputs = _backup_inputs(cfg)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": [
            {"arcname": arcname, "sha256": _sha256(path), "size": path.stat().st_size}
            for path, arcname in inputs
        ],
    }

    with ZipFile(out_path, "w", compression=ZIP_DEFLATED) as zf:
        for path, arcname in inputs:
            zf.write(path, arcname=arcname)
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    return out_path


def restore_backup(
    backup_zip: str,
    *,
    target_index_folder: str | None = None,
    target_config_path: str | None = None,
    verify_only: bool = False,
) -> dict:
    backup_path = Path(backup_zip)
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup archive not found: {backup_zip}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with ZipFile(backup_path, "r") as zf:
            zf.extractall(tmp_dir)
        manifest_path = tmp_dir / "manifest.json"
        if not manifest_path.exists():
            raise ValueError("Backup archive is missing manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest.get("files", []):
            extracted = tmp_dir / item["arcname"]
            if not extracted.exists():
                raise ValueError(f"Backup archive is missing {item['arcname']}")
            actual = _sha256(extracted)
            if actual != item["sha256"]:
                raise ValueError(f"Checksum mismatch for {item['arcname']}")

        restored: dict[str, str] = {}
        if not verify_only:
            if target_index_folder:
                index_dir = Path(target_index_folder)
                index_dir.mkdir(parents=True, exist_ok=True)
                for name in ("legal_sorter.db", "legal_sorter.db-wal", "legal_sorter.db-shm"):
                    src = tmp_dir / "index" / name
                    if src.exists():
                        dst = index_dir / name
                        shutil.copy2(src, dst)
                        restored[name] = str(dst)
            if target_config_path:
                src = tmp_dir / "config" / "config.yaml"
                if src.exists():
                    dst = Path(target_config_path)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    restored["config.yaml"] = str(dst)

        return {
            "verified_ok": True,
            "archive_path": str(backup_path),
            "restored": restored,
            "manifest": manifest,
        }
