from __future__ import annotations

import json
from pathlib import Path

from .database import DB
from . import config as cfgmod
from .watcher import scan_once

_SAMPLES = [
    {
        "label": "Smith v. Jones",
        "text": (
            "Smith v. Jones, 410 U.S. 113 (1973). Decided January 1, 2020 by the "
            "California Supreme Court. Docket No. 19-cv-00042. The court followed "
            "Brown v. Board of Education, 347 U.S. 483 (1954), and held that the "
            "regulation violated due process under the Fourteenth Amendment. "
            "Counsel for appellant argued that the district court erred in granting "
            "summary judgment. We hold that the district court's judgment is affirmed "
            "in part and reversed in part. The matter is remanded for further proceedings "
            "consistent with this opinion. Plaintiff's motion for injunctive relief is denied."
        ),
    },
    {
        "label": "Brown v. Board Follow-On",
        "text": (
            "Brown v. Board Follow-On, 600 U.S. 21 (2024). Decided February 2, 2024 "
            "by the Ninth Circuit Court of Appeals in California. Case No. 23-cv-01234. "
            "The panel distinguished 410 U.S. 113 (1973) but followed 347 U.S. 483 (1954) "
            "on equal protection principles under the Fourteenth Amendment. We conclude "
            "that the district court properly denied defendant's motion to dismiss. "
            "The court held that petitioner has standing to challenge the statute. "
            "Ordered that the judgment of the district court is affirmed. "
            "Counsel for respondent did not contest jurisdiction."
        ),
    },
    {
        "label": "Anderson v. Lopez",
        "text": (
            "Anderson v. Lopez, 555 U.S. 222 (2009). Filed September 9, 2022 before "
            "the Florida District Court. Docket No. 22-cv-00555. The court criticized "
            "410 U.S. 113 (1973) and limited its reasoning to criminal procedure disputes "
            "under the Fourth and Fifth Amendments. We hold that warrantless search of "
            "the defendant's vehicle was unconstitutional absent probable cause. "
            "Appellant's counsel argued that the stop violated Miranda v. Arizona, "
            "384 U.S. 436 (1966). The judgment is reversed and remanded for new trial. "
            "Defendant's motion for summary judgment is denied."
        ),
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
