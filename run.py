import sys
import os
import json
import traceback
from pathlib import Path
import yaml  # Assuming you use PyYAML for your config.yaml

# 1. FORCE UTF-8 ENCODING (Standardized for Windows Console)
if sys.platform == "win32" and getattr(sys.stdout, "reconfigure", None):
    sys.stdout.reconfigure(encoding='utf-8')

# 2. IMPORT WATCHER LOGIC
try:
    from src.watcher import run_forever
    from src.health_check import run_health_check
    from src import config as cfgmod
    from src.evaluation import evaluate_jsonl, check_quality_gate, load_baseline
    from src.backup import create_backup, restore_backup
    from src.database import DB
    from src.setup_wizard import run_cli_wizard, is_first_run
    from src.demo_data import load_demo_data

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

    def _load_cfg_soft() -> dict:
        try:
            cfgmod._CONFIG_CACHE = {}
            return cfgmod.load_config()
        except Exception:
            return {}

    # Execution Logic
    try:
        if args and args[0] == "health":
            print("[Command] Running local health check...")
            sys.exit(run_health_check())
        elif args and args[0] == "setup":
            sys.exit(run_cli_wizard())
        elif args and args[0] == "load-demo-data":
            result = load_demo_data()
            print(json.dumps(result, indent=2))
            sys.exit(0)
        elif args and args[0] == "rebuild-citations":
            cfgmod._CONFIG_CACHE = {}
            cfg = cfgmod.load_config()
            db = DB(str(Path(cfg["index_folder"]) / "legal_sorter.db"))
            try:
                result = db.rebuild_citation_relationships()
            finally:
                db.conn.close()
            print(json.dumps(result, indent=2))
            sys.exit(0)
        elif args and args[0] == "readiness":
            print("[Command] Running strict production-readiness check...")
            sys.exit(run_health_check(strict=True))
        elif args and args[0] == "evaluate":
            dataset = args[1] if len(args) > 1 else "tests/fixtures/gold_cases.jsonl"
            print(f"[Command] Running quality benchmark on: {dataset}")
            report = evaluate_jsonl(dataset)
            cfg = _load_cfg_soft()
            qg = (((cfg or {}).get("production", {}) or {}).get("quality_gate", {}) or {})
            citation_f1_min = float(qg.get("citation_f1_min", 0.70))
            entity_f1_min = float(qg.get("entity_f1_min", 0.60))
            min_cases = int(qg.get("min_cases", 1))
            baseline = None
            baseline_file = str(qg.get("baseline_file", "")).strip()
            if baseline_file and os.path.exists(baseline_file):
                baseline = load_baseline(baseline_file)
            ok, failures = check_quality_gate(
                report,
                citation_f1_min=citation_f1_min,
                entity_f1_min=entity_f1_min,
                min_cases=min_cases,
                baseline=baseline,
            )
            print(json.dumps(report, indent=2))
            if not ok:
                print("[QUALITY GATE] FAILED:")
                for fail in failures:
                    print(f"  - {fail}")
                sys.exit(1)
            print("[QUALITY GATE] PASSED")
            sys.exit(0)
        elif args and args[0] == "backup":
            print("[Command] Creating deterministic production backup...")
            cfgmod._CONFIG_CACHE = {}
            cfg = cfgmod.load_config(strict=True)
            out_path = create_backup(cfg)
            verified = restore_backup(str(out_path), verify_only=True)
            db = DB(str(os.path.join(cfg["index_folder"], "legal_sorter.db")))
            db.record_backup(
                backup_id=Path(out_path).stem,
                archive_path=str(out_path),
                verified_ok=bool(verified.get("verified_ok")),
                details={"created_at": verified.get("manifest", {}).get("created_at")},
            )
            db.conn.close()
            print(f"[BACKUP] Created: {out_path}")
            sys.exit(0)
        elif args and args[0] == "restore":
            if len(args) < 3:
                print("Usage: python run.py restore /absolute/path/to/backup.zip /absolute/path/to/index_folder [config_path]")
                sys.exit(1)
            backup_zip = args[1]
            index_folder = args[2]
            config_path = args[3] if len(args) > 3 else None
            result = restore_backup(
                backup_zip,
                target_index_folder=index_folder,
                target_config_path=config_path,
                verify_only=False,
            )
            print(json.dumps(result, indent=2))
            sys.exit(0)
        elif args and args[0] == "crawl":
            print("[Command] Received 'crawl' signal. Initiating watcher loop...")
            run_forever()
        else:
            if is_first_run():
                print("[Setup] First-run setup has not been completed yet.")
                print("[Setup] Starting the guided setup wizard...\n")
                code = run_cli_wizard()
                if code != 0:
                    sys.exit(code)
            print("[Command] No specific command received. Defaulting to standard run.")
            run_forever()
            
    except Exception as e:
        print("\n[CRITICAL ERROR] The engine has crashed:")
        print(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()