import sys
import os
import traceback
import yaml  # Assuming you use PyYAML for your config.yaml

# 1. FORCE UTF-8 ENCODING (Standardized for Windows Console)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

# 2. IMPORT WATCHER LOGIC
try:
    from src.watcher import run_forever

except ImportError as e:
    print(f"Error loading src.watcher: {e}")
    sys.exit(1)

def load_config():
    """Loads configuration if available."""
    config_path = "config.yaml"
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    return None

def main():
    # Capture command line arguments (e.g., 'crawl')
    args = sys.argv[1:]
    
    print("--------------------------------------------------")
    print("LegalSorter Engine | Initialization Sequence")
    print(f"Working Directory: {os.getcwd()}")
    print("--------------------------------------------------")

    # Load configuration
    config = load_config()
    if config:
        print("[System] Configuration file loaded successfully.")
    else:
        print("[Warning] No config.yaml found. Proceeding with defaults.")

    # Execution Logic
    try:
        if args and args[0] == "crawl":
            print("[Command] Received 'crawl' signal. Initiating watcher loop...")
            run_forever()
        else:
            print("[Command] No specific command received. Defaulting to standard run.")
            run_forever()
            
    except Exception as e:
        print("\n[CRITICAL ERROR] The engine has crashed:")
        print(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()