import os
from pathlib import Path
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
import yaml

def load_pull_folder_path(config_path="config.yaml"):
    """Reads config.yaml to find the exact targeted data folder."""
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}
            return config.get("pull_folder", "pull_folder")
    return "pull_folder"

def build_similarity_index():
    """
    Reads all text files in the configured pull folder, converts them into TF-IDF vectors,
    and prepares the dataset for similarity matching.
    """
    folder_name = load_pull_folder_path()
    pull_folder = Path(folder_name)
    
    if not pull_folder.exists():
        print(f"[!] Target folder '{folder_name}' does not exist yet.")
        print("[*] Note: Ensure your bulk_ingest.py script has run and successfully saved case files first!")
        return None, None
        
    file_paths = list(pull_folder.glob("bulk_*.txt"))
    if not file_paths:
        print(f"[!] Folder '{folder_name}' exists, but no 'bulk_*.txt' case files were found inside.")
        return None, None

    print(f"[*] Reading {len(file_paths)} cases from '{folder_name}' into the indexing engine...")
    documents = []
    valid_paths = []
    
    for path in file_paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read().strip()
                if text:
                    documents.append(text)
                    valid_paths.append(path)
        except Exception as e:
            print(f"[WARN] Skipping {path.name} due to read error: {e}")

    print("[*] Generating TF-IDF mathematical vectors...")
    vectorizer = TfidfVectorizer(max_features=10000, stop_words='english', strip_accents='unicode')
    tfidf_matrix = vectorizer.fit_transform(documents)
    
    print("[+] Index built successfully.")
    return valid_paths, tfidf_matrix

def find_similar_cases(target_file_name, file_paths, tfidf_matrix, top_n=5):
    """Takes a specific target file name and finds the top_n most conceptually similar cases."""
    target_idx = None
    for idx, path in enumerate(file_paths):
        if path.name == target_file_name:
            target_idx = idx
            break
            
    if target_idx is None:
        print(f"[!] Target file '{target_file_name}' not found in the index list.")
        return

    target_vector = tfidf_matrix[target_idx]
    similarities = cosine_similarity(target_vector, tfidf_matrix).flatten()

    related_indices = np.argsort(similarities)[::-1]
    
    print(f"\n=== Top {top_n} Most Similar Cases to: {target_file_name} ===")
    count = 0
    for idx in related_indices:
        if idx == target_idx:
            continue
        
        score = similarities[idx]
        if score > 0.05:
            matched_file = file_paths[idx]
            print(f"[{count + 1}] {matched_file.name} | Match Score: {score:.4f}")
            count += 1
            if count >= top_n:
                break
                
    if count == 0:
        print("[*] No closely matching legal concepts found in the current dataset batch.")
    print("======================================================\n")

if __name__ == "__main__":
    paths, matrix = build_similarity_index()
    if paths and matrix is not None:
        test_target = paths[0].name
        find_similar_cases(test_target, paths, matrix, top_n=3)