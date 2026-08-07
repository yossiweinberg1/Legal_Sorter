import os
import random
import requests
import yaml
from pathlib import Path


def run_spot_check():
    """
    Pulls a random local case file and compares it against the live
    CourtListener API to verify data integrity.
    """
    print("=== LegalSorter Integrity Auditor ===")

    # 1. Load config including API token(s) and pull_folder
    config_path = Path(__file__).resolve().parent / "config.yaml"
    try:
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        print("[!] Could not find or parse config.yaml.")
        return

    # Support both old single-token key and new list key
    cl_cfg = cfg.get("courtlistener", {})
    tokens = cl_cfg.get("api_tokens", [])
    if not tokens and cl_cfg.get("api_token"):
        tokens = [cl_cfg.get("api_token")]

    # Allow override via environment variable
    env_token = os.getenv("COURTLISTENER_API_TOKEN")
    if env_token:
        tokens = [env_token]

    if not tokens:
        print("[!] No API token found. Set COURTLISTENER_API_TOKEN or configure config.yaml.")
        return

    token = tokens[0]
    headers = {"Authorization": f"Token {token}"}

    # 2. Pick a random ingested case from the pull folder (read from config)
    pull_folder = Path(cfg.get("pull_folder", "pull_folder"))
    if not pull_folder.exists():
        print(f"[!] Pull folder '{pull_folder}' does not exist yet.")
        return

    all_files = list(pull_folder.glob("bulk_*.txt"))
    if not all_files:
        print("[!] No bulk_*.txt cases found in the pull folder to audit.")
        return

    target_file = random.choice(all_files)
    opinion_id = target_file.stem.replace("bulk_", "")

    print(f"[*] Randomly selected local case ID: {opinion_id}")

    # 3. Read the local text
    local_text = target_file.read_text(encoding="utf-8", errors="ignore").strip()

    # 4. Fetch the live ground-truth text from CourtListener
    base_url = cl_cfg.get("base_url", "https://www.courtlistener.com/api/rest/v4").rstrip("/")
    api_url = f"{base_url}/opinions/{opinion_id}/"
    print(f"[*] Fetching live ground-truth data from: {api_url}")

    try:
        response = requests.get(api_url, headers=headers, timeout=30)
    except Exception as e:
        print(f"[!] Network error: {e}")
        return

    if response.status_code != 200:
        print(f"[!] API Error: HTTP {response.status_code}")
        return

    live_data = response.json()
    live_text = (live_data.get("plain_text") or "").strip()

    # 5. The Verdict
    print("\n--- Audit Results ---")
    print(f"Local File:  {len(local_text):,} characters")
    print(f"Live Server: {len(live_text):,} characters")

    if not live_text:
        print("[WARNING] Live server returned no plain_text — cannot compare.")
    elif local_text == live_text:
        print("\n[SUCCESS] Local data perfectly matches the live online record. Zero corruption.")
    else:
        diff_chars = abs(len(local_text) - len(live_text))
        print(f"\n[WARNING] Discrepancy detected ({diff_chars:,} char difference).")
    print("---------------------\n")


if __name__ == "__main__":
    run_spot_check()
