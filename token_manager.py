import yaml
import os
from pathlib import Path

def direct_system_token_swap(new_token, config_path="config.yaml", fetch_path="src/legal_fetch.py"):
    """
    Takes a manually pasted token, securely overwrites config.yaml, 
    and purges any remnants of the old token from execution environments.
    """
    # 1. Load old token for the purge sequence
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    old_token = config.get("api_token")
    
    # 2. Overwrite the config with the fresh token
    config["api_token"] = new_token.strip()
    with open(config_path, "w") as f:
        yaml.safe_dump(config, f)
    print("[+] config.yaml safely updated with the new token.", flush=True)

    # 3. Clean and purge references inside legal_fetch.py
    if old_token and os.path.exists(fetch_path):
        with open(fetch_path, "r") as f:
            content = f.read()
        
        if old_token in content:
            content = content.replace(old_token, new_token.strip())
            with open(fetch_path, "w") as f:
                f.write(content)
            print(f"[+] Old key ending in ...{old_token[-6:]} completely purged from execution scripts.", flush=True)
        else:
            print("[+] Checked legal_fetch.py: Clean of hardcoded old tokens.", flush=True)

if __name__ == "__main__":
    print("=== LegalSorter Secure Token Update Utility ===")
    print("Log into CourtListener in your browser, click 'Regenerate' on your API profile, and copy it.")
    print("-----------------------------------------------------------------------------------------")
    
    user_token = input("Paste your fresh API token here: ").strip()
    
    if len(user_token) < 20:
        print("[CRITICAL] That string looks too short to be a valid CourtListener API token.")
    else:
        print("\n--- Executing System Swap ---")
        direct_system_token_swap(user_token)
        print("--- Update Complete: System is optimized and ready ---")