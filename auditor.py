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
    
    # 1. Load your API Token for the live check
    try:
        with open("config.yaml", "r") as f:
            token = yaml.safe_load(f)["api_token"]
    except Exception:
        print("[!] Could not find config.yaml to load API token.")
        return

    headers = {"Authorization": f"Token {token}"} #
    
    # 2. Pick a random ingested case from your pull folder
    pull_folder = Path("pull_folder")
    if not pull_folder.exists():
        print("[!] Pull folder does not exist yet.")
        return
        
    all_files = list(pull_folder.glob("bulk_*.txt"))
    if not all_files:
        print("[!] No cases found in the pull folder to audit.")
        return
        
    # Grab one random file and extract the ID from the filename
    target_file = random.choice(all_files)
    opinion_id = target_file.name.replace("bulk_", "").replace(".txt", "")
    
    print(f"[*] Randomly selected local case ID: {opinion_id}")
    
    # 3. Read the local text
    with open(target_file, "r", encoding="utf-8") as f:
        local_text = f.read().strip()
    
    # 4. Fetch the live ground-truth text from CourtListener
    print(f"[*] Fetching live ground-truth data from CourtListener...")
    api_url = f"https://www.courtlistener.com/api/rest/v4/opinions/{opinion_id}/" #
    
    response = requests.get(api_url, headers=headers)
    
    if response.status_code != 200:
        print(f"[!] API Error: Received status code {response.status_code}")
        return
        
    live_data = response.json()
    
    # CourtListener stores the clean string in 'plain_text'
    live_text = live_data.get("plain_text", "").strip() #
    
    # 5. The Verdict
    print("\n--- Audit Results ---")
    print(f"Local File: {len(local_text)} characters")
    print(f"Live Server: {len(live_text)} characters")
    
    if local_text == live_text:
        print("\n[SUCCESS] Local data perfectly matches the live online record. Zero corruption.")
    else:
        print("\n[WARNING] Discrepancy detected between local storage and live server.")
    print("---------------------\n")

if __name__ == "__main__":
    run_spot_check()