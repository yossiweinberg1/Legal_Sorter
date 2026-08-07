import sqlite3
import os
import sys
import threading
# Ensure Python can resolve internal module paths inside src/
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "src", "llm")))
from train import train_llm

def harvest_and_train():
    db_path = "legal_sorter.db"
    if not os.path.exists(db_path):
        print(f"❌ Database mapping file not located at target: {db_path}")
        return

    print("🔌 Extracting record corpora from database indexes...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Grab whatever text data column you store your raw legal documents in
    cursor.execute("SELECT text FROM documents WHERE text IS NOT NULL AND text != ''")
    case_texts = [row[0] for row in cursor.fetchall()]
    conn.close()

    if not case_texts:
        print("❌ Extraction failed: No text data found to train on.")
        return

    print(f"📚 Loaded {len(case_texts)} case documents into memory workspace.")
    train_llm(case_texts)

if __name__ == "__main__":
    harvest_and_train()