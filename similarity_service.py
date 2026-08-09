"""Similarity search service — builds and caches a TF-IDF index.

Index source priority:
  1. SQLite documents table (always up-to-date, works for both API and bulk modes)
  2. bulk_*.txt files in pull_folder (legacy fallback when DB is not accessible)

Cache files are stored next to this module so they survive restarts.
"""
import os
import re
import threading
from pathlib import Path

import joblib
import numpy as np
import yaml
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from src.database import LEGAL_SORTER_DB_PATH, connect_sqlite

MODULE_DIR = Path(__file__).resolve().parent

# Cache file locations
INDEX_FILE = MODULE_DIR / "similarity_matrix.joblib"
VECTORIZER_FILE = MODULE_DIR / "vectorizer.joblib"
PATHS_FILE = MODULE_DIR / "indexed_paths.joblib"
CACHE_LOCK = threading.RLock()


def _load_config(config_path: str = "config.yaml") -> dict:
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    return {}


def _clean_aliases(*values: str | None) -> list[str]:
    aliases: list[str] = []
    for value in values:
        if not value or not isinstance(value, str):
            continue
        value = value.strip()
        if value and value not in aliases:
            aliases.append(value)
    return aliases


def _bulk_filename_from_url(source_url: str | None) -> str | None:
    if not source_url:
        return None
    match = re.search(r"#(\d+)$", source_url)
    if not match:
        return None
    return f"bulk_{match.group(1)}.txt"


def _entry_label(entry: dict) -> str:
    return (
        entry.get("ref_no")
        or entry.get("barcode")
        or entry.get("filename")
        or entry.get("doc_id")
        or entry.get("label")
        or "unknown"
    )


def _load_from_db(cfg: dict, barcode_prefix: str | None = None) -> tuple[list[str], list[dict]]:
    """Load texts and matchable metadata from the SQLite documents table.

    Args:
        cfg:            Config dict.
        barcode_prefix: If given, only documents whose barcode starts with this
                        prefix are loaded.  Uses the B-tree index on barcode.
    """
    index_folder = cfg.get("index_folder", "")
    db_path = Path(index_folder) / "legal_sorter.db" if index_folder else Path(LEGAL_SORTER_DB_PATH)

    if not db_path or not db_path.exists():
        return [], []

    try:
        with connect_sqlite(str(db_path)) as conn:
            if barcode_prefix is not None:
                if "%" in barcode_prefix:
                    # Raw wildcard pattern from barcode_prefix() — use as-is
                    like_pattern = barcode_prefix
                else:
                    # Plain prefix — escape LIKE specials, then append %
                    like_pattern = (
                        barcode_prefix
                        .replace("\\", "\\\\")
                        .replace("%", "\\%")
                        .replace("_", "\\_")
                        + "%"
                    )
                rows = conn.execute(
                    "SELECT id, ref_no, source_url, source_path, barcode, "
                    "SUBSTR(text, 1, 20000) FROM documents "
                    "WHERE text IS NOT NULL AND text != '' "
                    "AND barcode LIKE ? ESCAPE '\\' "
                    "ORDER BY added_at DESC LIMIT 5000",
                    (like_pattern,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, ref_no, source_url, source_path, barcode, "
                    "SUBSTR(text, 1, 20000) FROM documents "
                    "WHERE text IS NOT NULL AND text != '' "
                    "ORDER BY added_at DESC LIMIT 5000"
                ).fetchall()
        texts: list[str] = []
        entries: list[dict] = []
        for doc_id, ref_no, source_url, source_path, barcode, text in rows:
            text = (text or "").strip()
            if not text:
                continue
            filename = Path(source_path).name if source_path else None
            entry = {
                "doc_id": doc_id,
                "ref_no": ref_no,
                "barcode": barcode,
                "source_url": source_url,
                "filename": filename,
            }
            entry["aliases"] = _clean_aliases(
                ref_no,
                barcode,
                doc_id,
                source_url,
                filename,
                _bulk_filename_from_url(source_url),
            )
            entry["label"] = _entry_label(entry)
            texts.append(text)
            entries.append(entry)
        return texts, entries
    except Exception:
        return [], []


def _load_from_files(cfg: dict) -> tuple[list[str], list[dict]]:
    """Load texts from bulk_*.txt files (legacy fallback)."""
    pull_folder = Path(cfg.get("pull_folder", "pull_folder"))
    if not pull_folder.exists():
        return [], []

    texts, entries = [], []
    for path in pull_folder.glob("bulk_*.txt"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                texts.append(text)
                entries.append(
                    {
                        "doc_id": None,
                        "ref_no": None,
                        "source_url": None,
                        "filename": path.name,
                        "aliases": [path.name],
                        "label": path.name,
                    }
                )
        except Exception:
            continue
    return texts, entries


def _normalize_entries(raw_entries) -> list[dict]:
    if not isinstance(raw_entries, list):
        return []

    normalized: list[dict] = []
    for item in raw_entries:
        if isinstance(item, str):
            normalized.append(
                {
                    "doc_id": None,
                    "ref_no": None,
                    "source_url": None,
                    "filename": item,
                    "aliases": [item],
                    "label": item,
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        entry["aliases"] = _clean_aliases(*(entry.get("aliases") or []), entry.get("label"))
        entry["label"] = _entry_label(entry)
        normalized.append(entry)
    return normalized


def _find_target_index(target_label: str, entries: list[dict]) -> int | None:
    target = (target_label or "").strip()
    if not target:
        return None
    for i, entry in enumerate(entries):
        if target == entry.get("label") or target in (entry.get("aliases") or []):
            return i
    return None


def build_and_cache_index() -> tuple[bool, str]:
    """Build the TF-IDF similarity index from the DB (or files as fallback) and cache it."""
    with CACHE_LOCK:
        cfg = _load_config()

        texts, entries = _load_from_db(cfg)
        source = "database"
        if not texts:
            texts, entries = _load_from_files(cfg)
            source = "pull_folder files"

        if not texts:
            return False, "No indexed documents found. Ingest cases first."

        vectorizer = TfidfVectorizer(
            max_features=25000, stop_words="english", strip_accents="unicode"
        )
        tfidf_matrix = vectorizer.fit_transform(texts)

        joblib.dump(tfidf_matrix, INDEX_FILE, compress=3)
        joblib.dump(vectorizer, VECTORIZER_FILE, compress=3)
        joblib.dump(entries, PATHS_FILE, compress=3)

        return True, f"Indexed {len(entries)} cases from {source}."


def _ensure_cache() -> tuple[bool, str]:
    """Return (ok, error_msg). Builds cache if it doesn't exist."""
    needed = [INDEX_FILE, VECTORIZER_FILE, PATHS_FILE]
    if all(os.path.exists(f) for f in needed):
        return True, ""
    return build_and_cache_index()


def get_similar_cases(target_label: str, top_n: int = 5) -> dict:
    """Return the top-n most similar cases to *target_label*.

    *target_label* may be a filename, ref_no, doc_id, or source_url.
    """
    with CACHE_LOCK:
        ok, err = _ensure_cache()
        if not ok:
            return {"error": err, "matches": []}

        tfidf_matrix = joblib.load(INDEX_FILE)
        entries = _normalize_entries(joblib.load(PATHS_FILE))

        target_idx = _find_target_index(target_label, entries)
        if target_idx is None:
            rebuilt, rebuild_message = build_and_cache_index()
            if not rebuilt:
                return {"error": rebuild_message, "matches": []}
            tfidf_matrix = joblib.load(INDEX_FILE)
            entries = _normalize_entries(joblib.load(PATHS_FILE))
            target_idx = _find_target_index(target_label, entries)
        if target_idx is None:
            return {"error": f"'{target_label}' not found in the current index.", "matches": []}

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
            entry = entries[idx]
            results.append(
                {
                    "label": entry.get("label"),
                    "ref_no": entry.get("ref_no"),
                    "barcode": entry.get("barcode"),
                    "filename": entry.get("filename"),
                    "doc_id": entry.get("doc_id"),
                    "source_url": entry.get("source_url"),
                    "score": round(score, 4),
                }
            )
            if len(results) >= top_n:
                break

        return {"error": None, "matches": results}


def get_similar_cases_filtered(
    target_label: str,
    barcode_prefix: str,
    top_n: int = 5,
) -> dict:
    """Hybrid search: filter by structured barcode prefix, then rank by TF-IDF similarity.

    This is the primary hybrid search entry point.  It:

    1. Loads only documents whose barcode starts with *barcode_prefix* directly
       from the database (O(log n) B-tree lookup — no full table scan).
    2. Builds an in-memory TF-IDF index from that filtered subset.
    3. Ranks results by cosine similarity to the target document.

    Because the filtered set is typically much smaller than the full corpus,
    this is both faster and more precise than running similarity over everything.

    Args:
        target_label:   A ref_no, barcode, doc_id, filename, or source_url that
                        identifies the query document.  The target must itself
                        be present inside the filtered set (its barcode must
                        also match the prefix).
        barcode_prefix: Structured ID prefix, e.g. ``"LS-CA-CA9-"`` (9th Circuit
                        cases only) or ``"LS-%-%-CON-"`` for constitutional law
                        across all courts.
        top_n:          Maximum number of similar cases to return.

    Returns:
        {"error": None, "matches": [...], "filtered_count": N}
        Each match: {label, ref_no, barcode, doc_id, source_url, filename, score}
        On failure: {"error": "...", "matches": [], "filtered_count": 0}
    """
    cfg = _load_config()
    texts, entries = _load_from_db(cfg, barcode_prefix=barcode_prefix)

    if not texts:
        return {
            "error": (
                f"No documents found matching barcode prefix '{barcode_prefix}'. "
                "Check the prefix or run 'Build Similarity Index' first."
            ),
            "matches": [],
            "filtered_count": 0,
        }

    entries = _normalize_entries(entries)
    target_idx = _find_target_index(target_label, entries)
    if target_idx is None:
        return {
            "error": (
                f"'{target_label}' not found within the barcode-filtered set "
                f"(prefix='{barcode_prefix}'). "
                "The target document may not match this prefix."
            ),
            "matches": [],
            "filtered_count": len(entries),
        }

    vectorizer = TfidfVectorizer(
        max_features=25000, stop_words="english", strip_accents="unicode"
    )
    tfidf_matrix = vectorizer.fit_transform(texts)

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
        entry = entries[idx]
        results.append(
            {
                "label": entry.get("label"),
                "ref_no": entry.get("ref_no"),
                "barcode": entry.get("barcode"),
                "filename": entry.get("filename"),
                "doc_id": entry.get("doc_id"),
                "source_url": entry.get("source_url"),
                "score": round(score, 4),
            }
        )
        if len(results) >= top_n:
            break

    return {"error": None, "matches": results, "filtered_count": len(entries)}
