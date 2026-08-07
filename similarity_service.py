import os
from pathlib import Path
import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import yaml

# Cache file locations inside your app directory
INDEX_FILE = "similarity_matrix.joblib"
VECTORIZER_FILE = "vectorizer.joblib"
PATHS_FILE = "indexed_paths.joblib"

def load_pull_folder_path(config_path="config.yaml"):
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}
            return config.get("pull_folder", "pull_folder")
    return "pull_folder"

def build_and_cache_index():
    """Reads disk files, builds the vector index, and saves it to disk for instant loading."""
    folder_name = load_pull_folder_path()
    pull_folder = Path(folder_name)
    
    if not pull_folder.exists():
        return False, f"Target folder '{folder_name}' does not exist."
        
    file_paths = list(pull_folder.glob("bulk_*.txt"))
    if not file_paths:
        return False, "No 'bulk_*.txt' case files found to index."

    documents = []
    valid_paths = []
    
    for path in file_paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read().strip()
                if text:
                    documents.append(text)
                    valid_paths.append(str(path.absolute()))
        except Exception:
            continue

    if not documents:
        return False, "No readable text content found."

    # Fit the vectorizer
    vectorizer = TfidfVectorizer(max_features=25000, stop_words='english', strip_accents='unicode')
    tfidf_matrix = vectorizer.fit_transform(documents)
    
    # Cache everything to disk with high compression
    joblib.dump(tfidf_matrix, INDEX_FILE, compress=3)
    joblib.dump(vectorizer, VECTORIZER_FILE, compress=3)
    joblib.dump(valid_paths, PATHS_FILE, compress=3)
    
    return True, f"Successfully indexed {len(valid_paths)} cases."

def get_similar_cases(target_file_name, top_n=5):
    """
    Instantly loads cached index matrix and returns top matches.
    Can be called directly by your UI app dashboard!
    """
    if not (os.path.exists(INDEX_FILE) and os.path.exists(VECTORIZER_FILE) and os.path.exists(PATHS_FILE)):
        # Fallback to build index if cache doesn't exist yet
        success, msg = build_and_cache_index()
        if not success:
            return {"error": msg, "matches": []}

    # High-speed load from cache
    tfidf_matrix = joblib.load(INDEX_FILE)
    file_paths = joblib.load(PATHS_FILE)

    target_idx = None
    for idx, path_str in enumerate(file_paths):
        if Path(path_str).name == target_file_name:
            target_idx = idx
            break
            
    if target_idx is None:
        return {"error": f"Target file '{target_file_name}' not in current index.", "matches": []}

    # Run quick cosine matrix multiplication
    target_vector = tfidf_matrix[target_idx]
    similarities = cosine_similarity(target_vector, tfidf_matrix).flatten()
    related_indices = np.argsort(similarities)[::-1]
    
    results = []
    for idx in related_indices:
        if idx == target_idx:
            continue
        
        score = float(similarities[idx])
        if score > 0.05:
            results.append({
                "filename": Path(file_paths[idx]).name,
                "score": round(score, 4)
            })
            if len(results) >= top_n:
                break
                
    return {"error": None, "matches": results}