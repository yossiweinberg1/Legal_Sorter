"""One-command local bootstrap for Legal Sorter."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def _venv_python() -> str:
    if os.name == "nt":
        return str(VENV / "Scripts" / "python.exe")
    return str(VENV / "bin" / "python")


def _run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(ROOT))


def main() -> int:
    if not VENV.exists():
        _run([sys.executable, "-m", "venv", str(VENV)])
    py = _venv_python()

    _run([py, "-m", "pip", "install", "--upgrade", "pip"])
    _run([py, "-m", "pip", "install", "-r", "requirements.txt"])
    health = subprocess.run([py, "run.py", "health"], cwd=str(ROOT), check=False)
    if health.returncode != 0:
        print("\nBootstrap failed: health check reported issues.")
        return health.returncode

    print("\nBootstrap complete.")
    print(f"Use interpreter: {py}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
