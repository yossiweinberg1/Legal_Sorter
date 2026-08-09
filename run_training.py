import os
import sys

# Ensure Python can resolve internal module paths inside src/llm/
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "src", "llm")))
from train import train_llm
from src.database import connect_sqlite, resolve_db_path


def harvest_and_train():
    try:
        db_path = resolve_db_path()
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return

    print(f"🔌 Connecting to database: {db_path}")
    conn = connect_sqlite(db_path)
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
