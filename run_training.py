import sqlite3
import os
import sys
from pathlib import Path

# Ensure Python can resolve internal module paths inside src/llm/
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "src", "llm")))
from train import train_llm


def _resolve_db_path() -> str:
    """Find the database using config.yaml so the path is never hardcoded."""
    try:
        from src import config as cfgmod
        cfg = cfgmod.load_config()
        return str(Path(cfg["index_folder"]) / "legal_sorter.db")
    except Exception:
        # Fallback: search common locations relative to this script
        candidates = [
                Path(__file__).parent / "legal_sorter.db",
                Path(__file__).parent / "index" / "legal_sorter.db",
            ]
        for p in candidates:
            if p.exists():
                return str(p)
        raise FileNotFoundError(
            "Could not locate legal_sorter.db. "
            "Check that index_folder is set correctly in config.yaml."
        )


def harvest_and_train():
    try:
        db_path = _resolve_db_path()
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return

    print(f"🔌 Connecting to database: {db_path}")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT text FROM documents WHERE text IS NOT NULL AND text != ''")
    case_texts = [row[0] for row in cursor.fetchall()]
    conn.close()

    if not case_texts:
        print("❌ Extraction failed: No text data found in the database.")
        return

    print(f"📚 Loaded {len(case_texts)} case documents into memory workspace.")
    train_llm(case_texts)


if __name__ == "__main__":
    harvest_and_train()
