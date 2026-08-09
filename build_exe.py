"""
build_exe.py — Build a standalone LegalSorter Windows .exe with PyInstaller.

Usage (run this from the repo root in your activated venv):

    python build_exe.py

The finished executable will be at:

    dist/LegalSorter/LegalSorter.exe   (one-folder mode, recommended)
    dist/LegalSorter.exe               (if --onefile flag is passed)

Pass --onefile for a single-file build (slower to start, easier to share):

    python build_exe.py --onefile

Requirements:
    pip install pyinstaller
    (all other deps must already be installed in the same venv)

Notes:
  - torch/CUDA wheels can be very large; the final folder may be 1-3 GB.
    For distribution without GPU support you can install the CPU-only torch:
        pip install torch --index-url https://download.pytorch.org/whl/cpu
  - The build embeds config.yaml, docs/, and src/ as data files so the exe
    finds them at runtime via sys._MEIPASS.
  - Run the finished exe from a writable directory; it writes the SQLite DB,
    Case_Library/, and logs next to itself at runtime.
"""

import subprocess
import sys
import shutil
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Build LegalSorter .exe")
    parser.add_argument(
        "--onefile",
        action="store_true",
        help="Produce a single .exe (slow start-up) instead of a one-folder bundle",
    )
    args = parser.parse_args()

    # Verify PyInstaller is available.
    if shutil.which("pyinstaller") is None:
        print("[ERROR] PyInstaller not found. Install it first:\n  pip install pyinstaller")
        sys.exit(1)

    mode_flag = ["--onefile"] if args.onefile else ["--onedir"]

    # Data files to bundle: (source_path, dest_folder_inside_bundle)
    datas = [
        ("config.yaml", "."),
        ("docs", "docs"),
        ("src", "src"),
    ]
    data_args: list[str] = []
    sep = ";" if sys.platform == "win32" else ":"
    for src, dst in datas:
        if Path(src).exists():
            data_args += ["--add-data", f"{src}{sep}{dst}"]

    # Hidden imports that PyInstaller's static analysis tends to miss.
    hidden = [
        "sklearn.utils._cython_blas",
        "sklearn.neighbors.typedefs",
        "sklearn.neighbors.quad_tree",
        "sklearn.tree._utils",
        "pywintypes",
        "win32api",
        "win32con",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "fastapi",
        "anyio",
        "anyio._backends._asyncio",
        "lxml.etree",
        "lxml._elementpath",
        "bs4",
    ]
    hidden_args: list[str] = []
    for h in hidden:
        hidden_args += ["--hidden-import", h]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        *mode_flag,
        "--name", "LegalSorter",
        "--windowed",                    # no console window (GUI app)
        "--icon", "legal_sorter.ico",    # use existing .ico when present
        *data_args,
        *hidden_args,
        "--noconfirm",                   # overwrite previous build
        "app.pyw",
    ]

    # Drop --icon arg if the .ico file does not exist yet; PyInstaller will use
    # its default icon rather than forcing a regeneration step.
    if not (ROOT / "legal_sorter.ico").exists():
        try:
            idx = cmd.index("--icon")
            # Remove both --icon and its value (the next element).
            del cmd[idx:idx + 2]
        except ValueError:
            pass  # flag was not present; nothing to do

    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)

    bundle = ROOT / "dist" / ("LegalSorter.exe" if args.onefile else "LegalSorter")
    print(f"\n[OK] Build complete: {bundle}")
    if not args.onefile:
        print("     Share the entire 'LegalSorter' folder — users run LegalSorter.exe inside it.")
    else:
        print("     Share the single LegalSorter.exe file.")


if __name__ == "__main__":
    main()
