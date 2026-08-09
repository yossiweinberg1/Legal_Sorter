"""
create_shortcut.py
------------------
Run this script once to install a desktop shortcut for LegalSorter on
Windows or Linux (macOS .app bundle support is a stub).

Usage:
    python create_shortcut.py

No third-party packages required.
"""

import os
import sys
import stat
import textwrap
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
APP_SCRIPT = APP_ROOT / "app.pyw"
ICON_PATH = APP_ROOT / "legal_sorter.ico"

# Import the shared icon utility; if for some reason src/ is not on the path
# yet (standalone usage before a full venv install), fall back gracefully.
try:
    sys.path.insert(0, str(APP_ROOT))
    from src.icon_utils import ensure_icon as _ensure_icon_fn
    def _ensure_icon() -> str:
        return _ensure_icon_fn(ICON_PATH, force=False)
except Exception:
    def _ensure_icon() -> str:  # type: ignore[misc]
        return str(ICON_PATH)  # icon may not exist; shortcut will show default


def _find_python_exe() -> str:
    """Return the best pythonw / python executable path."""
    candidates = [
        APP_ROOT / ".venv" / "Scripts" / "pythonw.exe",   # Windows venv
        APP_ROOT / ".venv" / "bin" / "python",             # Linux/macOS venv
        Path(sys.executable),                              # Running interpreter
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return "python"


def create_windows_shortcut():
    """Creates a .lnk desktop shortcut via the Windows Script Host COM object."""
    try:
        import winreg  # noqa: F401  (exists only on Windows)
    except ImportError:
        print("[skip] Not a Windows system.")
        return

    try:
        import win32com.client as wsc
        shell = wsc.Dispatch("WScript.Shell")
        desktop = Path(shell.SpecialFolders("Desktop"))
        lnk_path = desktop / "LegalSorter.lnk"
        shortcut = shell.CreateShortCut(str(lnk_path))
        shortcut.TargetPath = _find_python_exe().replace("python.exe", "pythonw.exe")
        shortcut.Arguments = f'"{APP_SCRIPT}"'
        shortcut.WorkingDirectory = str(APP_ROOT)
        shortcut.Description = "LegalSorter — AI-powered legal case organiser"
        shortcut.WindowStyle = 1
        shortcut.IconLocation = _ensure_icon()
        shortcut.Save()
        print(f"[OK] Windows shortcut created: {lnk_path}")
    except ImportError:
        # Fall back to dropping the .vbs launcher on the desktop
        desktop = Path.home() / "Desktop"
        desktop.mkdir(parents=True, exist_ok=True)
        vbs_dest = desktop / "LegalSorter.vbs"
        vbs_src = APP_ROOT / "LegalSorter.vbs"
        if vbs_src.exists():
            import shutil
            shutil.copy2(str(vbs_src), str(vbs_dest))
            print(f"[OK] VBS launcher copied to desktop: {vbs_dest}")
        else:
            print("[warn] pywin32 not installed and LegalSorter.vbs not found — "
                  "install pywin32 (`pip install pywin32`) for a proper .lnk shortcut.")


def create_linux_shortcut():
    """Installs the .desktop file into ~/.local/share/applications and ~/Desktop."""
    desktop_file = APP_ROOT / "LegalSorter.desktop"
    if not desktop_file.exists():
        print("[warn] LegalSorter.desktop not found in app root — cannot install.")
        return

    python_exe = _find_python_exe()
    exec_line = f"{python_exe} {APP_SCRIPT}"

    content = textwrap.dedent(f"""\
        [Desktop Entry]
        Version=1.0
        Type=Application
        Name=LegalSorter
        GenericName=Legal Case Organizer
        Comment=AI-powered legal case indexer and sorter
        Exec=bash -c 'cd "{APP_ROOT}" && {exec_line}'
        Icon=legal-sorter
        Terminal=false
        Categories=Office;Science;
        Keywords=legal;law;cases;sorter;
        StartupNotify=true
    """)

    apps_dir = Path.home() / ".local" / "share" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    target = apps_dir / "LegalSorter.desktop"
    target.write_text(content, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    print(f"[OK] Application entry installed: {target}")

    # Also place on ~/Desktop if it exists
    user_desktop = Path.home() / "Desktop"
    if user_desktop.is_dir():
        desk_target = user_desktop / "LegalSorter.desktop"
        desk_target.write_text(content, encoding="utf-8")
        desk_target.chmod(desk_target.stat().st_mode | stat.S_IXUSR)
        print(f"[OK] Desktop icon created:        {desk_target}")


def create_macos_stub():
    """Prints instructions for macOS (full .app bundle is out of scope here)."""
    print(
        "[macOS] To launch LegalSorter, run:\n"
        f"  cd '{APP_ROOT}' && python app.pyw\n\n"
        "For a clickable Dock icon, drag the Terminal running the app into the Dock,\n"
        "or use Automator to wrap the command in an Application bundle."
    )


def main():
    print(f"LegalSorter — Desktop Shortcut Creator")
    print(f"App root: {APP_ROOT}\n")

    if sys.platform == "win32":
        create_windows_shortcut()
    elif sys.platform == "darwin":
        create_macos_stub()
    else:
        create_linux_shortcut()


if __name__ == "__main__":
    main()
