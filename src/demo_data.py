from __future__ import annotations

import json
from pathlib import Path

from .database import DB
from . import config as cfgmod
from .watcher import scan_once

_SAMPLES = [
    {
        "label": "Smith v. Jones",
        "text": "Smith v. Jones, 410 U.S. 113 (1973). Decided January 1, 2020 by the California Supreme Court. The court followed Brown v. Board, 347 U.S. 483 (1954), and held that the regulation violated due process.",
    },
    {
        "label": "Brown v. Board Follow-On",
        "text": "Brown v. Board Follow-On, 600 U.S. 21 (2024). Decided February 2, 2024 by the Ninth Circuit in California. The panel distinguished 410 U.S. 113 (1973) but followed 347 U.S. 483 (1954) on equal protection principles.",
    },
    {
        "label": "Anderson v. Lopez",
        "text": "Anderson v. Lopez, 555 U.S. 222 (2009). Filed September 9, 2022 before the Florida District Court. The court criticized 410 U.S. 113 (1973) and limited its reasoning to criminal procedure disputes.",
    },
]


def load_demo_data() -> dict:
    cfg = cfgmod.load_config()
    pull_folder = Path(cfg["pull_folder"])
    pull_folder.mkdir(parents=True, exist_ok=True)
    for idx, sample in enumerate(_SAMPLES, 1):
        path = pull_folder / f"demo_case_{idx}.txt"
        path.write_text(sample["text"], encoding="utf-8")
        Path(str(path) + ".meta.json").write_text(
            json.dumps(
                {
                    "source_url": f"demo://sample/{idx}",
                    "case_name": sample["label"],
                }
            ),
            encoding="utf-8",
        )
    db = DB(str(Path(cfg["index_folder"]) / "legal_sorter.db"))
    try:
        scan_once(cfg, db)
        total = db.safe_execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        db.conn.close()
    return {"loaded": len(_SAMPLES), "total_cases": int(total or 0)}
