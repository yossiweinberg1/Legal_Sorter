from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
ENV_FILE = ROOT / ".env"
STATE_FILE = ROOT / ".legal_sorter_setup.json"


def _venv_python() -> str:
    if os.name == "nt":
        return str(VENV / "Scripts" / "python.exe")
    return str(VENV / "bin" / "python")


def default_paths() -> dict[str, str]:
    home = Path.home()
    if sys.platform == "win32":
        base = home / "Documents" / "LegalSorter"
    elif sys.platform == "darwin":
        base = home / "Documents" / "LegalSorter"
    else:
        base = home / ".local" / "share" / "legal_sorter"
    return {
        "pull_folder": str(base / "pull"),
        "index_folder": str(base / "index"),
        "pending_folder": str(base / "pending"),
    }


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def mark_setup_complete(details: dict | None = None) -> None:
    payload = {
        "completed": True,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "details": details or {},
    }
    STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def is_first_run() -> bool:
    state = _load_state()
    if state.get("completed"):
        return False
    try:
        from . import config as cfgmod

        cfg = cfgmod.load_config()
        db_path = Path(cfg["index_folder"]) / "legal_sorter.db"
        if db_path.exists():
            return False
    except Exception:
        pass
    return True


def _upsert_env(values: dict[str, str]) -> None:
    existing_lines = []
    existing_map: dict[str, str] = {}
    if ENV_FILE.exists():
        existing_lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        for line in existing_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            existing_map[key.strip()] = value

    def _render(value: str) -> str:
        raw = str(value)
        escaped = raw.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    for key, value in values.items():
        existing_map[key] = _render(value)

    emitted = set()
    output: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in emitted:
            continue
        output.append(f"{key}={existing_map[key]}")
        emitted.add(key)
    for key, value in existing_map.items():
        if key not in emitted:
            output.append(f"{key}={value}")
    if not output:
        output = [f"{k}={v}" for k, v in existing_map.items()]
    ENV_FILE.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def ensure_virtualenv(log=None) -> str:
    def emit(msg: str) -> None:
        if log:
            log(msg)
        else:
            print(msg)

    if not VENV.exists():
        emit("Creating local virtual environment...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)], cwd=str(ROOT))
    py = _venv_python()
    emit("Installing/updating dependencies...")
    subprocess.check_call([py, "-m", "pip", "install", "--upgrade", "pip"], cwd=str(ROOT))
    subprocess.check_call([py, "-m", "pip", "install", "-r", "requirements.txt"], cwd=str(ROOT))
    return py


def run_health_check(python_exec: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [python_exec, "run.py", "health"],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, output


def load_demo_data(python_exec: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [python_exec, "run.py", "load-demo-data"],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, output


def _llm_env_from_choice(choice: str) -> dict[str, str]:
    choice = (choice or "").strip().lower()
    if choice == "ollama":
        return {
            "LEGAL_SORTER_LLM_BASE_URL": "http://localhost:11434/v1",
            "LEGAL_SORTER_LLM_MODEL": "llama3",
            "LEGAL_SORTER_LLM_FAST_MODEL": "llama3",
            "LEGAL_SORTER_LLM_ACCURATE_MODEL": "llama3",
            "LLM_API_KEY": "ollama",
        }
    if choice == "lmstudio":
        return {
            "LEGAL_SORTER_LLM_BASE_URL": "http://localhost:1234/v1",
            "LEGAL_SORTER_LLM_MODEL": "local-model",
            "LEGAL_SORTER_LLM_FAST_MODEL": "local-model",
            "LEGAL_SORTER_LLM_ACCURATE_MODEL": "local-model",
            "LLM_API_KEY": "lm-studio",
        }
    return {}


def _apply_basic_setup(paths: dict[str, str], courtlistener_token: str, llm_choice: str) -> dict[str, str]:
    env_updates = {
        "LEGAL_SORTER_PULL_FOLDER": paths["pull_folder"],
        "LEGAL_SORTER_INDEX_FOLDER": paths["index_folder"],
        "LEGAL_SORTER_PENDING_FOLDER": paths["pending_folder"],
    }
    if courtlistener_token.strip():
        env_updates["COURTLISTENER_API_TOKEN"] = courtlistener_token.strip()
    env_updates.update(_llm_env_from_choice(llm_choice))
    _upsert_env(env_updates)
    for path in paths.values():
        Path(path).mkdir(parents=True, exist_ok=True)
    return env_updates


def run_cli_wizard() -> int:
    print("LegalSorter Setup Wizard")
    print("========================")
    print("This guided setup will prepare folders, install dependencies, run a health check, and optionally load demo data.\n")
    defaults = default_paths()
    py = ensure_virtualenv()
    print("\nChoose where LegalSorter should keep its working folders.")
    chosen = {}
    for key, default in defaults.items():
        raw = input(f"{key.replace('_', ' ').title()} [{default}]: ").strip()
        chosen[key] = raw or default
    token = input("\nOptional CourtListener API token (press Enter to skip): ").strip()
    print("\nOptional local AI setup:")
    print("  1) Skip for now")
    print("  2) Ollama")
    print("  3) LM Studio")
    llm_raw = input("Choose 1, 2, or 3 [1]: ").strip() or "1"
    llm_choice = {"1": "", "2": "ollama", "3": "lmstudio"}.get(llm_raw, "")
    _apply_basic_setup(chosen, token, llm_choice)
    ok, output = run_health_check(py)
    print("\nHealth check result:\n")
    print(output or "(no output)")
    if not ok:
        print("\nSetup stopped because the health check failed. Review the messages above, fix the issue, and re-run `python setup_wizard.py --cli`.")
        return 1
    if input("\nLoad demo/sample data now? [y/N]: ").strip().lower().startswith("y"):
        demo_ok, demo_output = load_demo_data(py)
        print(demo_output or "(no output)")
        if not demo_ok:
            print("\nDemo data could not be loaded, but the basic setup completed successfully.")
    mark_setup_complete({"mode": "cli", "paths": chosen})
    print("\nSetup complete. Start the desktop app with:")
    print(f"  {py} app.pyw")
    return 0


def run_gui_wizard(parent=None) -> bool:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog

    owns_root = parent is None
    root = parent or tk.Tk()
    if owns_root:
        root.withdraw()

    def ask(msg: str) -> bool:
        return bool(messagebox.askyesno("LegalSorter Setup Wizard", msg, parent=parent or root))

    messagebox.showinfo(
        "LegalSorter Setup Wizard",
        "Welcome. This wizard will prepare LegalSorter for first use, set up folders, run a health check, and optionally load demo data.",
        parent=parent or root,
    )
    try:
        py = ensure_virtualenv()
    except Exception as exc:
        messagebox.showerror(
            "Setup Error",
            f"LegalSorter could not create or update the local Python environment.\n\nDetails:\n{exc}\n\nPlease install Python 3.10+ and re-run the wizard.",
            parent=parent or root,
        )
        if owns_root:
            root.destroy()
        return False

    defaults = default_paths()
    base_default = str(Path(defaults["pull_folder"]).parent)
    selected_base = filedialog.askdirectory(
        title="Choose a folder for LegalSorter data",
        initialdir=base_default,
        mustexist=False,
        parent=parent or root,
    ) or base_default
    selected_base_path = Path(selected_base)
    chosen = {
        "pull_folder": str(selected_base_path / "pull"),
        "index_folder": str(selected_base_path / "index"),
        "pending_folder": str(selected_base_path / "pending"),
    }
    token = simpledialog.askstring(
        "CourtListener (Optional)",
        "Paste a CourtListener API token if you have one.\n\nYou can leave this blank and add it later.",
        parent=parent or root,
    ) or ""
    llm_choice = simpledialog.askstring(
        "Local AI (Optional)",
        "Local AI setup:\n\nType 'ollama' for Ollama,\n'lmstudio' for LM Studio,\nor leave blank to skip for now.",
        parent=parent or root,
    ) or ""
    _apply_basic_setup(chosen, token, llm_choice)
    ok, output = run_health_check(py)
    if not ok:
        messagebox.showerror(
            "Health Check Failed",
            f"Setup could not finish because the health check failed.\n\n{output}",
            parent=parent or root,
        )
        if owns_root:
            root.destroy()
        return False
    messagebox.showinfo(
        "Health Check Passed",
        output or "Health check passed.",
        parent=parent or root,
    )
    if ask("Would you like to load a few demo cases so you can see the system working right away?"):
        demo_ok, demo_output = load_demo_data(py)
        if demo_ok:
            messagebox.showinfo("Demo Data Loaded", demo_output or "Demo data loaded.", parent=parent or root)
        else:
            messagebox.showwarning("Demo Data", demo_output or "Demo data could not be loaded.", parent=parent or root)
    mark_setup_complete({"mode": "gui", "paths": chosen})
    messagebox.showinfo(
        "Setup Complete",
        "LegalSorter is ready.\n\nNext steps:\n- Open the desktop app\n- Drop files into your pull folder\n- Use the AI features once your local or cloud model is configured",
        parent=parent or root,
    )
    if owns_root:
        root.destroy()
    return True


def run_setup(prefer_gui: bool = True) -> int:
    if prefer_gui:
        try:
            return 0 if run_gui_wizard() else 1
        except Exception:
            pass
    return run_cli_wizard()


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    prefer_gui = "--cli" not in argv
    return run_setup(prefer_gui=prefer_gui)
