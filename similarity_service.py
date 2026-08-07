"""Similarity search service — builds and caches a TF-IDF index.

Index source priority:
  1. SQLite documents table (always up-to-date, works for both API and bulk modes)
  2. bulk_*.txt files in pull_folder (legacy fallback when DB is not accessible)

Cache files are stored next to this module so they survive restarts.
"""
import os
from pathlib import Path
import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import sqlite3
import yaml

# Cache file locations
INDEX_FILE = "similarity_matrix.joblib"
VECTORIZER_FILE = "vectorizer.joblib"
PATHS_FILE = "indexed_paths.joblib"


def _load_config(config_path: str = "config.yaml") -> dict:
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    return {}


def _load_from_db(cfg: dict) -> tuple[list[str], list[str]]:
    """Load texts and labels from the SQLite documents table."""
    index_folder = cfg.get("index_folder", "")
    db_path = Path(index_folder) / "legal_sorter.db" if index_folder else None

    if not db_path or not db_path.exists():
        return [], []

    try:
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT id, ref_no, SUBSTR(text, 1, 20000) FROM documents "
                "WHERE text IS NOT NULL AND text != '' ORDER BY added_at DESC LIMIT 5000"
            ).fetchall()
        texts = [r[2] for r in rows if r[2].strip()]
        labels = [f"{r[1] or r[0][:12]}" for r in rows if r[2].strip()]
        return texts, labels
    except Exception:
        return [], []


def _load_from_files(cfg: dict) -> tuple[list[str], list[str]]:
    """Load texts from bulk_*.txt files (legacy fallback)."""
    pull_folder = Path(cfg.get("pull_folder", "pull_folder"))
    if not pull_folder.exists():
        return [], []

    texts, labels = [], []
    for path in pull_folder.glob("bulk_*.txt"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                texts.append(text)
                labels.append(path.name)
        except Exception:
            continue
    return texts, labels


def build_and_cache_index() -> tuple[bool, str]:
    """Build the TF-IDF similarity index from the DB (or files as fallback) and cache it."""
    cfg = _load_config()

    texts, labels = _load_from_db(cfg)
    source = "database"
    if not texts:
        texts, labels = _load_from_files(cfg)
        source = "pull_folder files"

    if not texts:
        return False, "No indexed documents found. Ingest cases first."

    vectorizer = TfidfVectorizer(
        max_features=25000, stop_words="english", strip_accents="unicode"
    )
    tfidf_matrix = vectorizer.fit_transform(texts)

    joblib.dump(tfidf_matrix, INDEX_FILE, compress=3)
    joblib.dump(vectorizer, VECTORIZER_FILE, compress=3)
    joblib.dump(labels, PATHS_FILE, compress=3)

    return True, f"Indexed {len(labels)} cases from {source}."


def _ensure_cache() -> tuple[bool, str]:
    """Return (ok, error_msg). Builds cache if it doesn't exist."""
    needed = [INDEX_FILE, VECTORIZER_FILE, PATHS_FILE]
    if all(os.path.exists(f) for f in needed):
        return True, ""
    return build_and_cache_index()


def get_similar_cases(target_label: str, top_n: int = 5) -> dict:
    """Return the top-n most similar cases to *target_label*.

    *target_label* is either a filename (``bulk_12345.txt``) or a ref_no
    (``LC-000001``) — whichever was stored in the index.
    """
    ok, err = _ensure_cache()
    if not ok:
        return {"error": err, "matches": []}

    tfidf_matrix = joblib.load(INDEX_FILE)
    labels = joblib.load(PATHS_FILE)

    # Match by filename OR ref_no
    target_idx = next(
        (i for i, lbl in enumerate(labels) if lbl == target_label), None
    )
    if target_idx is None:
        return {
            "error": f"'{target_label}' not found in the current index. Rebuild the index first.",
            "matches": [],
        }

    target_vector = tfidf_matrix[target_idx]
    similarities = cosine_similarity(target_vector, tfidf_matrix).flatten()
    ranked = np.argsort(similarities)[::-1]

    results = []
    for idx in ranked:
        if idx == target_idx:
            continue
        score = float(similarities[idx])
        if score <= 0.05:
            break
        results.append({"label": labels[idx], "score": round(score, 4)})
        if len(results) >= top_n:
            break

    return {"error": None, "matches": results}
