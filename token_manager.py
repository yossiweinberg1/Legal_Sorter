"""Secure token update utility.

Updates the CourtListener API token in config.yaml ONLY.
Never reads or modifies source code files.

Usage:
    python token_manager.py

Or set the environment variable directly (preferred for CI/automation):
    Windows:  setx COURTLISTENER_API_TOKEN "your-token-here"
    Linux/Mac: export COURTLISTENER_API_TOKEN="your-token-here"
"""
import yaml
import os
from pathlib import Path


def update_config_token(new_token: str, config_path: str = "config.yaml") -> None:
    """Write the new token into config.yaml under courtlistener.api_tokens.

    The token is stored as a single-element list to match the multi-token
    format the rest of the app expects.  Never writes to source code files.
    """
    config_path = Path(config_path)

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    # Ensure the courtlistener section exists
    if "courtlistener" not in config:
        config["courtlistener"] = {}

    config["courtlistener"]["api_tokens"] = [new_token.strip()]

    # Remove the legacy single-token key if present
    config["courtlistener"].pop("api_token", None)

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, default_flow_style=False)

    print(f"[+] Token saved to {config_path}.", flush=True)
    print(
        "[!] Tip: For better security, use the environment variable instead:\n"
        "       Windows:  setx COURTLISTENER_API_TOKEN \"your-token\"\n"
        "       Linux/Mac: export COURTLISTENER_API_TOKEN=\"your-token\"",
        flush=True,
    )


if __name__ == "__main__":
    print("=== LegalSorter Secure Token Update Utility ===")
    print("Log into CourtListener, regenerate your API token, and paste it below.")
    print("-" * 60)

    user_token = input("Paste your CourtListener API token here: ").strip()

    if len(user_token) < 20:
        print("[CRITICAL] That string looks too short to be a valid CourtListener API token.")
    else:
        update_config_token(user_token)
        print("--- Token updated. Run the app normally. ---")
