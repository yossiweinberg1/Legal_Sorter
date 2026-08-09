#!/usr/bin/env python3
"""Barcode re-generation tool for LegalSorter.

Finds documents with missing, failed, or low-confidence barcodes and
re-generates them using the configured strategy (LLM or rules).

Usage
-----
    # Re-generate only missing / low-confidence barcodes (default threshold 0.85)
    python regen_barcodes.py

    # Use a custom confidence threshold
    python regen_barcodes.py --min-confidence 0.70

    # Force re-generation of ALL barcodes, including confirmed ones
    python regen_barcodes.py --force

    # Use a non-default database path
    python regen_barcodes.py --db path/to/legal_sorter.db

    # Dry-run: show what would be re-generated without making any changes
    python regen_barcodes.py --dry-run

Exit codes
----------
    0  All candidates succeeded (or no candidates found).
    1  One or more barcodes still failed after re-generation attempt.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Ensure the project root is on the path so relative imports work whether this
# script is run from the project root or from within src/.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("regen_barcodes")


def _default_db_path() -> str:
    """Mirror the logic in web_app.py / config.py to locate the database."""
    try:
        from src.config import load_config
        cfg = load_config()
        index_folder = cfg.get("index_folder", ".")
        return str(Path(index_folder) / "legal_sorter.db")
    except Exception:
        return "legal_sorter.db"


def _load_config() -> dict:
    try:
        from src.config import load_config
        return load_config()
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-generate missing or low-confidence barcodes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Path to the SQLite database (default: from config.yaml).",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=None,
        metavar="FLOAT",
        help=(
            "Re-generate barcodes with confidence below this value "
            "(default: barcode.confirm_threshold from config.yaml, or 0.85)."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-generate even manually confirmed barcodes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show candidates without making any changes.",
    )
    args = parser.parse_args()

    cfg = _load_config()
    barcode_cfg = cfg.get("barcode", {})

    db_path = args.db or _default_db_path()
    min_confidence = args.min_confidence
    if min_confidence is None:
        min_confidence = float(barcode_cfg.get("confirm_threshold", 0.85))
    confirm_threshold = float(barcode_cfg.get("confirm_threshold", 0.85))

    log.info("Database  : %s", db_path)
    log.info("Threshold : %.2f", min_confidence)
    log.info("Force     : %s", args.force)
    log.info("Dry-run   : %s", args.dry_run)

    try:
        from src.database import DB
        from src import barcode as barcode_mod
    except ImportError as exc:
        log.error("Could not import project modules: %s", exc)
        return 1

    db = DB(db_path)
    docs = db.find_docs_needing_barcode_regen(
        min_confidence=min_confidence,
        force=args.force,
    )

    if not docs:
        log.info("No documents require barcode re-generation.")
        return 0

    log.info("Found %d document(s) to re-generate.", len(docs))

    if args.dry_run:
        for doc in docs:
            log.info(
                "  [DRY-RUN] %s  ref=%s  barcode=%s  conf=%s",
                doc["id"][:12],
                doc["ref_no"],
                doc["barcode"] or "(none)",
                doc["barcode_confidence"],
            )
        return 0

    succeeded = 0
    failed = 0
    failed_ids: list[str] = []

    for doc in docs:
        doc_id = doc["id"]
        ref_no = doc["ref_no"] or ""
        try:
            entities = json.loads(doc["entities_json"]) if doc["entities_json"] else {}
            keywords = json.loads(doc["keywords_json"]) if doc["keywords_json"] else []
            bc, strategy, confidence = barcode_mod.assign_barcode(
                text=doc["text"] or "",
                entities=entities,
                citations=[],
                keywords=keywords,
                ref_no=ref_no,
                cfg=cfg,
                virtual_folder=doc["virtual_folder"] or "",
            )
            final_bc = db.set_barcode(
                doc_id, bc, strategy=strategy,
                confidence=confidence,
                confirm_threshold=confirm_threshold,
            )
            log.info(
                "  [OK] %s  ref=%s  barcode=%s  strategy=%s  conf=%.2f",
                doc_id[:12], ref_no, final_bc, strategy, confidence,
            )
            succeeded += 1
        except Exception as exc:
            log.warning("  [FAIL] %s  ref=%s  error=%s", doc_id[:12], ref_no, exc)
            failed += 1
            failed_ids.append(doc_id[:12])

    log.info("─" * 60)
    log.info("Succeeded : %d", succeeded)
    log.info("Failed    : %d", failed)
    if failed_ids:
        log.warning("Failed IDs: %s", ", ".join(failed_ids))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
