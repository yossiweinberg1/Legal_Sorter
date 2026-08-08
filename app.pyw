import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import shutil
import sqlite3
import json
import os
import sys
import subprocess
import time
import threading
import re
import webbrowser
from pathlib import Path
from src.watcher import TOKEN_REGISTRY, initialize_token_registry
from src import config as cfgmod
from similarity_service import build_and_cache_index, get_similar_cases
from src.legal_fetch import CourtListenerClient
from src.study_assistant import generate_study_response, NO_DOCS_SENTINEL, NO_MATCH_SENTINEL
from src.legal_ai import query_cases, analyze_case, semantic_search

# Import the window class from the new file you just created
from error_ledger import ErrorLedgerWindow

LEDGER_FILE = "ui_extraction_errors.json"


def _safe_console_print(message: str) -> None:
    stream = sys.stdout
    if stream is None:
        return
    try:
        stream.write(f"{message}\n")
        stream.flush()
    except Exception:
        pass


def _subprocess_creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _safe_traceback_print() -> None:
    if sys.stderr is None:
        return
    try:
        import traceback
        traceback.print_exc(file=sys.stderr)
    except Exception:
        pass


if getattr(sys.stdout, "reconfigure", None):
    sys.stdout.reconfigure(encoding="utf-8")

# =====================================================================
# 🛡️ ANTI-HANG & DIAGNOSTIC BOOTSTRAPPER (Windows ARM64 / PyTorch Safe)
# =====================================================================
_safe_console_print("\n[Bootloader] Initializing system environment...")

# Disable physical CUDA/GPU driver scans to prevent ARM64 driver deadlocks
os.environ["CUDA_VISIBLE_DEVICES"] = ""
# Restrict OpenMP thread allocations during startup initialization
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
_safe_console_print("[Bootloader] Thread and GPU environmental blocks secured.")

# Resolve and append the internal model script path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "src", "llm")))
_safe_console_print("[Bootloader] Source paths appended to environment.")

# Diagnostic pre-import sequence
try:
    _safe_console_print("[Bootloader] Safe-loading PyTorch core...")
    start_t = time.time()
    import torch
    _safe_console_print(f"[Bootloader] PyTorch loaded successfully in {time.time() - start_t:.2f}s.")
    
    _safe_console_print("[Bootloader] Safe-loading training dependencies...")
    start_t = time.time()
    import train
    _safe_console_print(f"[Bootloader] Training matrix loaded successfully in {time.time() - start_t:.2f}s.")
except Exception as e:
    _safe_console_print(f"[Bootloader ❌ ERROR] Pre-import sequence failed: {e}")
    _safe_traceback_print()

_safe_console_print("[Bootloader] All systems green. Initializing Tkinter window...\n")

# =====================================================================
# Icon helpers
# =====================================================================
from src.icon_utils import ensure_icon as _ensure_icon, ensure_logo_png as _ensure_logo_png

_ICON_PATH = Path(__file__).resolve().parent / "legal_sorter.ico"
_LOGO_PNG_PATH = Path(__file__).resolve().parent / "legal_sorter.png"

# Pre-generate the icon assets once so the shortcut creator can also reference them
try:
    _ensure_icon(_ICON_PATH)
    _ensure_logo_png(_LOGO_PNG_PATH, size=256)
except Exception:
    pass

# =====================================================================
class LegalSorterApp:
    def __init__(self, root):
        self.root = root
        self.crawler_process = None
        self.session_start_time = None
        self.last_total_indexed = -1 
        self.db_path = None
        self.log_session_start_mark = "1.0"
        self._temp_files = [] # track temp files created by repull so we can clean them up on exit
        self.ai_stop_requested = False
        self.log_autoscroll_var = tk.BooleanVar(value=True)

        # Window Configurations
        self.root.title(" LegalSorter Control Center")
        self.root.geometry("1380x860")
        self.root.minsize(1120, 740)
        self._app_icon = None
        self.apply_branding()
        
        self.stop_event = threading.Event()
        
        # Configure Polished Visual Styles
        self.configure_styles()
        self.build_menu()
        self.setup_shortcuts()
        
        # Discover and link the live DB file location
        self.discover_database_path()
        
        # Initialize UI Layout and Automation Hooks
        self.build_ui()
        self.schedule_auto_refresh()

        # Intercept window closing to clean up background processes safely
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def apply_branding(self):
        """Apply built-in branding: proper .ico on Windows, PhotoImage elsewhere."""
        # ── Windows: use the .ico file for title-bar AND taskbar ──────────────
        if sys.platform == "win32":
            try:
                self.root.wm_iconbitmap(_ensure_icon(_ICON_PATH))
                self._app_icon = None
                return
            except Exception:
                pass  # fall through to PhotoImage fallback

        # ── Other platforms: use the shared PNG logo ──────────────────────────
        try:
            self._app_icon = tk.PhotoImage(file=_ensure_logo_png(_LOGO_PNG_PATH, size=256))
            self.root.iconphoto(True, self._app_icon)
        except Exception:
            self._app_icon = None

    def build_menu(self):
        """Builds a simple modern command menu for frequently used actions."""
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Sync Database", command=self.sync_database, accelerator="Ctrl+R")
        file_menu.add_command(label="Open Selected Case", command=self.open_selected_case, accelerator="Ctrl+O")
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label="Focus Search", command=self.focus_search, accelerator="Ctrl+F")
        view_menu.add_command(label="Reset Workspace", command=self.reset_workspace)
        view_menu.add_command(label="Toggle Curation Panel", command=self.toggle_curation_deck, accelerator="Ctrl+Shift+C")
        menubar.add_cascade(label="View", menu=view_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Start Crawler", command=self.start_crawler_action)
        tools_menu.add_command(label="Stop Crawler", command=self.stop_crawler_action)
        tools_menu.add_command(label="Open Database Viewer", command=self.open_database_viewer)
        tools_menu.add_command(label="Copy Engine Log", command=self.copy_log_action, accelerator="Ctrl+Shift+L")
        menubar.add_cascade(label="Tools", menu=tools_menu)

        settings_menu = tk.Menu(menubar, tearoff=0)
        settings_menu.add_command(label="⚙️ API Key Manager", command=self.open_api_key_manager)
        settings_menu.add_command(label="🖥️ Create Desktop Shortcut", command=self._create_desktop_shortcut)
        settings_menu.add_separator()
        settings_menu.add_command(label="Toggle Log Auto-scroll",
                                  command=lambda: self.log_autoscroll_var.set(
                                      not self.log_autoscroll_var.get()))
        menubar.add_cascade(label="Settings", menu=settings_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(
            label="Keyboard Shortcuts",
            command=lambda: messagebox.showinfo(
                "Keyboard Shortcuts",
                "Ctrl+F  → Focus Search\n"
                "Ctrl+R  → Sync Database\n"
                "Ctrl+O  → Open Selected Case\n"
                "Ctrl+Shift+C → Toggle Curation\n"
                "Ctrl+Shift+L → Copy Log Since Start\n"
                "Ctrl+Shift+K → API Key Manager",
            ),
        )
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

    def setup_shortcuts(self):
        """Registers keyboard shortcuts for common actions."""
        self.root.bind("<Control-f>", lambda _e: self.focus_search())
        self.root.bind("<Control-r>", lambda _e: self.sync_database())
        self.root.bind("<Control-o>", lambda _e: self.open_selected_case())
        self.root.bind("<Control-Shift-C>", lambda _e: self.toggle_curation_deck())
        self.root.bind("<Control-Shift-L>", lambda _e: self.copy_log_action())
        self.root.bind("<Control-Shift-K>", lambda _e: self.open_api_key_manager())

    def focus_search(self):
        """Moves input focus to the explorer search box."""
        try:
            self.main_notebook.select(0)
            self.search_entry.focus_set()
            self.search_entry.icursor(tk.END)
        except Exception:
            pass

    def reset_workspace(self):
        """Resets quick filters and refreshes the explorer view."""
        try:
            self.quick_search_var.set("")
        except Exception:
            pass
        try:
            self.search_var.set("")
        except Exception:
            pass
        try:
            self.refresh_case_tree()
        except Exception:
            pass

    def apply_quick_search(self):
        """Applies quick search text into the explorer filter and refreshes."""
        try:
            q = self.quick_search_var.get().strip()
            self.search_var.set(q)
            self.focus_search()
            self.refresh_case_tree()
        except Exception:
            pass

    def run_health_check_action(self):
        """Runs local health check command in a non-blocking subprocess."""
        try:
            py = sys.executable
            proc = subprocess.run(
                [py, "run.py", "health"],
                capture_output=True,
                text=True,
                check=False,
                creationflags=_subprocess_creationflags(),
            )
            output = (proc.stdout or "").strip() or (proc.stderr or "").strip() or "No output."
            messagebox.showinfo("Health Check", output)
        except Exception as err:
            messagebox.showerror("Health Check", f"Failed to run health check: {err}")

    def get_error_badge_text(self):
        """Reads the error log and returns the formatted badge text (up to 99, then 99+)."""
        p = Path(LEDGER_FILE)
        if not p.exists():
            return "⚠️ Alerts (0)"
        
        try:
            with open(p, "r", encoding="utf-8") as f:
                errors = json.load(f)
                count = len(errors)
        except Exception:
            # Failsafe if file is being written to by the backend watcher
            return "⚠️ Alerts"

        if count == 0:
            return "⚠️ Alerts (0)"
        elif count > 99:
            return "⚠️ Alerts (99+)"
        else:
            return f"⚠️ Alerts ({count})"

    def open_ledger(self):
        """Opens the ledger window and refreshes the badge when closed."""
        ledger_win = ErrorLedgerWindow(self.root, ledger_path=LEDGER_FILE)
        
        # Wait until the ledger window is closed, then force an immediate update
        self.root.wait_window(ledger_win)
        self.update_alert_button()

    def update_alert_button(self):
        """Updates the button text and color based on current errors."""
        badge_text = self.get_error_badge_text()
        self.alert_btn.config(text=badge_text)
        
        # Visual cue: Turn the text orange/red if there are active errors
        if "Alerts (0)" in badge_text:
            self.style.configure("Alert.TButton", foreground="black", font=("Segoe UI", 9))
        else:
            self.style.configure("Alert.TButton", foreground="red", font=("Segoe UI", 9, "bold"))
            
        # Run this check again in 3000ms (3 seconds) to keep it updated in real-time
        self.root.after(3000, self.update_alert_button)

    def make_cl_client(self):
        """Build a CourtListenerClient using the first configured token."""
        cfg = cfgmod.load_config()
        token = cfg.get("courtlistener", {}).get("api_tokens", [None])[0]
        base = cfg.get("courtlistener", {}).get("base_url", "https://www.courtlistener.com/api/rest/v4")
        return CourtListenerClient(api_token=token, base_url=base)

    def _show_case_viewer(self, title: str, content: str, source_url: str = ""):
        """Opens a styled read-only Toplevel window to display case text."""
        win = tk.Toplevel(self.root)
        win.title(f"📄 {title}")
        win.geometry("900x700")
        win.minsize(640, 480)
        p = self._pal

        # Header bar
        header = ttk.Frame(win, padding=(10, 6))
        header.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(header, text=title, font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)

        # Toolbar
        toolbar = ttk.Frame(win, padding=(6, 2))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        copy_status = ttk.Label(toolbar, text="", foreground="#2ecc71", font=("Segoe UI", 9))

        def _copy_all():
            win.clipboard_clear()
            win.clipboard_append(content)
            copy_status.config(text="✅ Copied to clipboard.")
            win.after(3000, lambda: copy_status.config(text=""))

        def _copy_selection():
            try:
                sel = text_widget.get(tk.SEL_FIRST, tk.SEL_LAST)
            except tk.TclError:
                sel = ""
            if sel:
                win.clipboard_clear()
                win.clipboard_append(sel)
                copy_status.config(text=f"✅ Copied {len(sel)} chars.")
                win.after(3000, lambda: copy_status.config(text=""))

        def _open_url():
            if source_url and source_url.startswith("http"):
                webbrowser.open(source_url)

        ttk.Button(toolbar, text="📋 Copy All", command=_copy_all).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(toolbar, text="📋 Copy Selection", command=_copy_selection).pack(side=tk.LEFT, padx=(0, 4))
        if source_url and source_url.startswith("http"):
            ttk.Button(toolbar, text="🌐 Open Source URL", command=_open_url).pack(side=tk.LEFT, padx=(0, 4))
        copy_status.pack(side=tk.LEFT, padx=8)
        ttk.Button(toolbar, text="✖ Close", command=win.destroy).pack(side=tk.RIGHT)

        # Separator
        ttk.Separator(win, orient="horizontal").pack(fill=tk.X, padx=6)

        # Text body
        text_frame = ttk.Frame(win)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        text_widget = tk.Text(
            text_frame, wrap=tk.WORD, font=("Consolas", 10),
            bg="#1a1a2e", fg="#e0e0f0",
            insertbackground="#e0e0f0", relief="flat", borderwidth=0,
            padx=12, pady=8,
        )
        text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        vsb = ttk.Scrollbar(text_frame, command=text_widget.yview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        text_widget.config(yscrollcommand=vsb.set)

        text_widget.insert(tk.END, content)
        text_widget.config(state=tk.DISABLED)

        # Status bar at bottom
        status_bar = ttk.Label(win, text=f"  {len(content):,} characters  |  {len(content.splitlines()):,} lines",
                               font=("Segoe UI", 8), foreground="gray", anchor="w")
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        win.focus_set()

    def open_case_and_repull(self, doc_id: str):
        """
        Unified opener: Checks for a valid CourtListener URL to repull.
        If bulk data or missing URL, it shows the text directly in the app viewer.
        """
        if not self.db_path:
            return False, "Database path not configured."

        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT source_url, ref_no, text FROM documents WHERE id=?", (doc_id,))
            row = cur.fetchone()
            conn.close()
        except Exception as e:
            return False, f"DB error: {e}"

        if not row:
            return False, "Document not found in DB."

        source_url, ref_no, text_content = row

        # FALLBACK: If bulk data or missing URL, show text in the in-app viewer
        if not source_url or source_url.startswith("bulk://"):
            if not text_content:
                return False, "No URL available and no text archived in database."
            label = ref_no or doc_id
            self.root.after(0, lambda: self._show_case_viewer(
                title=label, content=text_content, source_url=source_url or ""
            ))
            return True, label

        # NORMAL REPULL: If standard HTTP URL exists, download and show in viewer
        client = self.make_cl_client()
        ok, result = client.download_to_temp(source_url, ref_no)
        if not ok:
            return False, result

        tmp_path = result
        if tmp_path not in self._temp_files:
            self._temp_files.append(tmp_path)

        # Read the downloaded file and display in-app
        try:
            with open(tmp_path, "r", encoding="utf-8", errors="replace") as fh:
                file_content = fh.read()
        except Exception:
            file_content = f"[Binary or unreadable file — saved to: {tmp_path}]"

        label = ref_no or doc_id
        self.root.after(0, lambda: self._show_case_viewer(
            title=label, content=file_content, source_url=source_url
        ))
        return True, tmp_path

    def cleanup_temp_files(self):
        """Remove any temp files we created earlier. Call this from on_close."""
        for p in list(self._temp_files):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        self._temp_files = []

    def on_close(self):
        """Ensures background threads are fully killed and temp files removed when closing the window."""
        try:
            self.stop_crawler()
        except Exception:
            pass

        try:
            self.cleanup_temp_files()
        except Exception:
            pass

        try:
            self.root.destroy()
        except Exception:
            os._exit(0)

    def configure_styles(self):
        """Injects clean spacing and consistent layout styling rules across widgets."""
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        # ── Palette ──────────────────────────────────────────────────
        BG        = "#1e1e2e"   # deep navy
        PANEL     = "#2a2a3e"   # slightly lighter panel
        ACCENT    = "#7c83e0"   # soft indigo accent
        ACCENT2   = "#4ade80"   # green success
        FG        = "#cdd6f4"   # light text
        FG_DIM    = "#888aaa"   # dim label text
        SEL_BG    = "#3b3b58"   # selection background
        ENTRY_BG  = "#313244"   # input field background
        BTN_BG    = "#363654"   # button background
        BTN_ACTIVE= "#4a4a72"   # button hover

        # Root window
        self.root.configure(bg=BG)

        # General frame / label / entry
        self.style.configure("TFrame",      background=BG)
        self.style.configure("TLabelframe", background=PANEL, foreground=FG,
                             relief="flat", borderwidth=1)
        self.style.configure("TLabelframe.Label", background=PANEL, foreground=ACCENT,
                             font=("Segoe UI", 9, "bold"))
        self.style.configure("TLabel",      background=BG, foreground=FG,
                             font=("Segoe UI", 10))
        self.style.configure("Dim.TLabel",  background=BG, foreground=FG_DIM,
                             font=("Segoe UI", 9, "italic"))

        # Buttons
        self.style.configure("TButton",
                             background=BTN_BG, foreground=FG,
                             font=("Segoe UI", 9),
                             relief="flat", padding=(8, 4))
        self.style.map("TButton",
                       background=[("active", BTN_ACTIVE), ("disabled", "#2a2a3e")],
                       foreground=[("disabled", "#555577")])

        # Accent (primary action) button
        self.style.configure("Accent.TButton",
                             background=ACCENT, foreground="#ffffff",
                             font=("Segoe UI", 9, "bold"),
                             relief="flat", padding=(8, 4))
        self.style.map("Accent.TButton",
                       background=[("active", "#9499f0"), ("disabled", "#3a3a5a")])

        # Success button
        self.style.configure("Success.TButton",
                             background="#245e3e", foreground=ACCENT2,
                             font=("Segoe UI", 9, "bold"),
                             relief="flat", padding=(8, 4))
        self.style.map("Success.TButton",
                       background=[("active", "#2e7a50")])

        # Alert button
        self.style.configure("Alert.TButton",
                             background=BTN_BG, foreground=FG,
                             font=("Segoe UI", 9))
        self.style.map("Alert.TButton",
                       background=[("active", BTN_ACTIVE)])

        # Entry
        self.style.configure("TEntry",
                             fieldbackground=ENTRY_BG, foreground=FG,
                             insertcolor=FG, borderwidth=0)

        # Treeview
        self.style.configure("Treeview",
                             background=PANEL, foreground=FG,
                             fieldbackground=PANEL,
                             rowheight=26, font=("Segoe UI", 10),
                             borderwidth=0)
        self.style.configure("Treeview.Heading",
                             background=SEL_BG, foreground=ACCENT,
                             font=("Segoe UI", 10, "bold"))
        self.style.map("Treeview",
                       background=[("selected", SEL_BG)],
                       foreground=[("selected", "#ffffff")])

        # Notebook
        self.style.configure("TNotebook",
                             background=BG, borderwidth=0)
        self.style.configure("TNotebook.Tab",
                             background=PANEL, foreground=FG_DIM,
                             font=("Segoe UI", 9), padding=(12, 5))
        self.style.map("TNotebook.Tab",
                       background=[("selected", BG)],
                       foreground=[("selected", ACCENT)])

        # Scrollbar
        self.style.configure("TScrollbar",
                             background=PANEL, troughcolor=BG,
                             arrowcolor=FG_DIM, borderwidth=0)

        # Separator
        self.style.configure("TSeparator", background=SEL_BG)

        # Store palette for use in Text widgets and dynamic styling
        self._pal = {
            "BG": BG, "PANEL": PANEL, "ACCENT": ACCENT, "ACCENT2": ACCENT2,
            "FG": FG, "FG_DIM": FG_DIM, "SEL_BG": SEL_BG,
            "ENTRY_BG": ENTRY_BG, "BTN_BG": BTN_BG,
        }

    def discover_database_path(self):
        """Resolves live operational database files using project configs and fallbacks."""
        try:
            cfg = cfgmod.load_config()
            if "index_folder" in cfg:
                self.db_path = os.path.abspath(os.path.join(cfg["index_folder"], "legal_sorter.db"))
        except Exception:
            pass
        if not self.db_path or not os.path.exists(self.db_path):
            self.db_path = os.path.abspath(os.path.join("index", "legal_sorter.db"))
        if not os.path.exists(self.db_path):
            self.db_path = os.path.abspath("legal_sorter.db")

    def _make_text(self, parent, **kw) -> tk.Text:
        """Factory for styled dark Text widgets."""
        p = self._pal
        defaults = dict(
            bg=p["PANEL"], fg=p["FG"],
            insertbackground=p["FG"],
            selectbackground=p["SEL_BG"],
            selectforeground="#ffffff",
            relief="flat", borderwidth=0,
            font=("Segoe UI", 10),
        )
        defaults.update(kw)
        return tk.Text(parent, **defaults)

    def build_ui(self):
        """Assembles the application main control board layout."""
        p = self._pal

        # ── Top header bar ────────────────────────────────────────────
        header = ttk.Frame(self.root, padding=(10, 6))
        header.pack(side=tk.TOP, fill=tk.X)

        # Alert badge (right-aligned first so other items push from left)
        self.alert_btn = ttk.Button(
            header, text="⚠️ Alerts (0)",
            command=self.open_ledger, style="Alert.TButton"
        )
        self.alert_btn.pack(side=tk.RIGHT, padx=6)
        self.update_alert_button()

        # Stat labels
        lbl_font = ("Segoe UI", 10, "bold")
        self.stat_indexed = ttk.Label(header, text="📂 Indexed: …", font=lbl_font)
        self.stat_indexed.pack(side=tk.LEFT, padx=12)
        self.stat_size = ttk.Label(header, text="💾 0.00 MB", font=lbl_font)
        self.stat_size.pack(side=tk.LEFT, padx=12)
        self.stat_queue = ttk.Label(header, text="⏳ Queue: 0", font=lbl_font)
        self.stat_queue.pack(side=tk.LEFT, padx=12)
        self.stat_timer = ttk.Label(header, text="⏱ 00:00:00", font=lbl_font,
                                    foreground=p["FG_DIM"])
        self.stat_timer.pack(side=tk.LEFT, padx=12)

        # ── Quick command strip ───────────────────────────────────────
        quick_bar = ttk.Frame(self.root, padding=(10, 0, 10, 4))
        quick_bar.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(quick_bar, text="Quick Search:").pack(side=tk.LEFT, padx=(0, 6))
        self.quick_search_var = tk.StringVar()
        self.quick_search_entry = ttk.Entry(quick_bar, textvariable=self.quick_search_var, width=38)
        self.quick_search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.quick_search_entry.bind("<Return>", lambda _e: self.apply_quick_search())

        ttk.Button(quick_bar, text="Apply", command=self.apply_quick_search, style="Accent.TButton").pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(quick_bar, text="Reset", command=self.reset_workspace).pack(side=tk.LEFT, padx=4)
        ttk.Button(quick_bar, text="Health", command=self.run_health_check_action).pack(side=tk.LEFT, padx=4)

        # ── Action button row ─────────────────────────────────────────
        btn_bar = ttk.Frame(self.root, padding=(10, 0, 10, 4))
        btn_bar.pack(side=tk.TOP, fill=tk.X)

        self.btn_stop_crawler  = ttk.Button(btn_bar, text="⏹ Stop",
                                            command=self.stop_crawler_action)
        self.btn_start_crawler = ttk.Button(btn_bar, text="▶ Start Crawler",
                                            command=self.start_crawler_action,
                                            style="Success.TButton")
        self.btn_sync          = ttk.Button(btn_bar, text="🔄 Sync",
                                            command=self.sync_database)
        self.btn_copy_log      = ttk.Button(btn_bar, text="📋 Copy Log",
                                            command=self.copy_log_action)
        self.btn_view_db       = ttk.Button(btn_bar, text="🗄 Database",
                                            command=self.open_database_viewer)
        self.btn_open_repull   = ttk.Button(btn_bar, text="📄 Open Case",
                                            command=self.open_selected_case)

        self._curation_expanded = False
        self.btn_toggle_curation = ttk.Button(btn_bar, text="⚙️ Curation ➕",
                                              command=self.toggle_curation_deck)

        for w in (self.btn_start_crawler, self.btn_stop_crawler, self.btn_sync,
                  self.btn_copy_log, self.btn_view_db, self.btn_open_repull,
                  self.btn_toggle_curation):
            w.pack(side=tk.LEFT, padx=4, pady=3)

        # ── Curation sub-bar (hidden by default) ─────────────────────
        self.curation_frame = ttk.Frame(self.root, padding=(10, 2))
        self.btn_bookmark    = ttk.Button(self.curation_frame, text="⭐ Bookmark",
                                          command=self.toggle_current_bookmark)
        self.btn_user_folder = ttk.Button(self.curation_frame, text="📁 Set Folder",
                                          command=self.prompt_user_folder)
        self.btn_delete_folder = ttk.Button(self.curation_frame, text="❌ Delete Folder",
                                            command=self.prompt_delete_folder)
        for w in (self.btn_bookmark, self.btn_user_folder, self.btn_delete_folder):
            w.pack(side=tk.LEFT, padx=6, pady=2)

        # ── Body container ────────────────────────────────────────────
        self.body_container = ttk.Frame(self.root)
        self.body_container.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 4))

        # ── Legacy Local-LLM panel (untouched, kept at bottom) ────────
        ai_frame = ttk.LabelFrame(self.body_container, text="🤖 Local LLM Insights Engine")
        ai_frame.pack(side=tk.BOTTOM, fill=tk.X, expand=False, padx=5, pady=(8, 4))

        ttk.Label(ai_frame, text="Ask AI about this case:").pack(
            anchor=tk.W, padx=10, pady=(5, 0))
        self.ai_prompt_entry = ttk.Entry(ai_frame, font=("Segoe UI", 10))
        self.ai_prompt_entry.pack(fill=tk.X, padx=10, pady=5)
        self.ai_prompt_entry.insert(0, "Summarize the key precedent and rulings of this case.")

        ai_buttons_layout = ttk.Frame(ai_frame)
        ai_buttons_layout.pack(fill=tk.X, padx=10, pady=5)
        self.btn_ai_generate = ttk.Button(ai_buttons_layout, text="✨ Generate Analysis",
                                          command=self.trigger_ai_inference)
        self.btn_ai_generate.pack(side=tk.LEFT, padx=2)
        self.btn_ai_train = ttk.Button(ai_buttons_layout, text="⚙️ Train LLM on DB",
                                       command=self.trigger_ai_training)
        self.btn_ai_train.pack(side=tk.LEFT, padx=2)
        self.btn_ai_stop = ttk.Button(ai_buttons_layout, text="🛑 Stop AI",
                                      command=self.abort_ai_execution)
        self.btn_ai_stop.pack(side=tk.LEFT, padx=2)

        self.ai_status_var = tk.StringVar(value="Status: Engine Idle")
        ttk.Label(ai_frame, textvariable=self.ai_status_var,
                  style="Dim.TLabel").pack(anchor=tk.W, padx=10, pady=(0, 5))

        # ── Main paned window ─────────────────────────────────────────
        self.paned_window = tk.PanedWindow(
            self.body_container, orient=tk.VERTICAL,
            sashrelief=tk.FLAT, sashwidth=4,
            bg=p["SEL_BG"]
        )
        self.paned_window.pack(fill=tk.BOTH, expand=True)

        # ── TOP PANE: Notebook with two tabs ─────────────────────────
        top_pane = ttk.Frame(self.paned_window, padding=4)
        self.paned_window.add(top_pane, height=520)

        self.main_notebook = ttk.Notebook(top_pane)
        self.main_notebook.pack(fill=tk.BOTH, expand=True)

        # ── TAB 1: Archive Explorer ───────────────────────────────────
        tab_explorer = ttk.Frame(self.main_notebook, padding=4)
        self.main_notebook.add(tab_explorer, text="  📚 Archive Explorer  ")
        self._build_explorer_tab(tab_explorer)

        # ── TAB 2: 🧠 AI Assistant ────────────────────────────────────
        tab_ai = ttk.Frame(self.main_notebook, padding=4)
        self.main_notebook.add(tab_ai, text="  🧠 AI Assistant  ")
        self._build_ai_assistant_tab(tab_ai)

        # ── BOTTOM PANE: Engine log ───────────────────────────────────
        log_frame = ttk.LabelFrame(self.paned_window,
                                   text=" Live Engine Output ", padding=6)
        self.paned_window.add(log_frame)

        # Log toolbar: autoscroll + copy buttons + status flash label
        log_toolbar = ttk.Frame(log_frame)
        log_toolbar.pack(side=tk.TOP, fill=tk.X, pady=(0, 4))

        ttk.Checkbutton(
            log_toolbar, text="Auto-scroll",
            variable=self.log_autoscroll_var,
        ).pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(
            log_toolbar, text="📋 Copy Since Start",
            command=self.copy_log_since_start,
        ).pack(side=tk.LEFT, padx=(0, 4))

        ttk.Button(
            log_toolbar, text="📋 Copy All",
            command=self.copy_full_log,
        ).pack(side=tk.LEFT, padx=(0, 4))

        ttk.Button(
            log_toolbar, text="🗑 Clear Log",
            command=self._clear_log,
        ).pack(side=tk.LEFT, padx=(0, 4))

        self._log_status_label = ttk.Label(
            log_toolbar, text="", foreground="#2ecc71", font=("Segoe UI", 9)
        )
        self._log_status_label.pack(side=tk.LEFT, padx=8)

        text_row = ttk.Frame(log_frame)
        text_row.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(
            text_row, wrap=tk.WORD, font=("Consolas", 10),
            height=10, bg="#12121e", fg="#c9cfe8",
            insertbackground="#c9cfe8", relief="flat", borderwidth=0
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.log_text.bind("<Key>", lambda e: "break")

        self.log_text.tag_config("error",   foreground="#ff5252",
                                 font=("Consolas", 10, "bold"))
        self.log_text.tag_config("warning", foreground="#ffb300")
        self.log_text.tag_config("info",    foreground="#4baffa")
        self.log_text.tag_config("success", foreground="#2ecc71",
                                 font=("Consolas", 10, "bold"))
        self.log_text.tag_config("system",  foreground="#666888")
        self.log_text.insert(tk.END, "[System Idle] Ready to stream updates.\n")

        log_scroll = ttk.Scrollbar(text_row, command=self.log_text.yview)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=log_scroll.set)

    # -----------------------------------------------------------------
    # TAB BUILDERS
    # -----------------------------------------------------------------

    def _build_explorer_tab(self, parent):
        """3-column layout: queue/control | tree | brief panel."""
        p = self._pal

        # COLUMN 1: Priority Queue + token controls
        queue_frame = ttk.LabelFrame(parent, text=" Network Control & Priority Targets ")
        queue_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=False)

        self.queue_text = self._make_text(queue_frame, width=28, height=10,
                                          font=("Consolas", 10))
        self.queue_text.pack(side=tk.TOP, fill=tk.X, expand=False, pady=(0, 4))
        self.queue_text.insert(tk.END, "Queue empty.")
        self.queue_text.config(state=tk.DISABLED)

        try:
            cfg = cfgmod.load_config()
            initialize_token_registry(cfg)
            api_label_frame = ttk.LabelFrame(queue_frame, text=" Live API Token Toggles ")
            api_label_frame.pack(side=tk.BOTTOM, fill=tk.X, expand=False, ipady=4)
            for token, meta in TOKEN_REGISTRY.items():
                masked_sig = f"...{token[-6:]}" if len(token) > 6 else token
                var = tk.BooleanVar(value=meta["enabled"])
                def make_toggle_callback(t=token, v=var):
                    return lambda: self.execute_token_toggle(t, v)
                tk.Checkbutton(
                    api_label_frame,
                    text=f"Key: {masked_sig}",
                    variable=var,
                    command=make_toggle_callback(),
                    bg=p["PANEL"], fg=p["FG"],
                    selectcolor=p["SEL_BG"],
                    activebackground=p["PANEL"],
                    relief="flat", font=("Segoe UI", 9),
                ).pack(anchor="w", padx=10, pady=2)
        except Exception as e:
            ttk.Label(queue_frame, text=f"API Panel Error: {e}",
                      foreground="#ff5252").pack(side=tk.BOTTOM)

        self.active_engine = tk.StringVar(value="API")
        engine_frame = ttk.LabelFrame(queue_frame, text=" Active Ingestion Engine ")
        engine_frame.pack(side=tk.BOTTOM, fill=tk.X, expand=False, ipady=4, pady=(0, 8))
        rb_kw = dict(bg=p["PANEL"], fg=p["FG"], selectcolor=p["SEL_BG"],
                     activebackground=p["PANEL"], relief="flat",
                     font=("Segoe UI", 9))
        tk.Radiobutton(engine_frame, text="API Tracker (Live targeted fetches)",
                       variable=self.active_engine, value="API",
                       command=self.switch_engine, **rb_kw).pack(anchor="w", padx=10, pady=2)
        tk.Radiobutton(engine_frame, text="S3 Bulk Dump (High-volume streaming)",
                       variable=self.active_engine, value="BULK",
                       command=self.switch_engine, **rb_kw).pack(anchor="w", padx=10, pady=2)
        ttk.Button(
            engine_frame,
            text="📁 Import Local Files…",
            command=self.import_local_legal_files,
        ).pack(anchor="w", padx=10, pady=(4, 6))

        # COLUMN 2: Case tree with search bar
        self.explorer_frame = ttk.LabelFrame(parent, text=" Crossover Library Matrix ")
        self.explorer_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=False, padx=8)

        search_frame = ttk.Frame(self.explorer_frame)
        search_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)
        ttk.Label(search_frame, text="🔍").pack(side=tk.LEFT, padx=(0, 4))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_case_tree())
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=28)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.case_tree = ttk.Treeview(self.explorer_frame, show="tree", selectmode="browse")
        self.case_tree.column("#0", width=310, minwidth=240)
        self.case_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.case_tree.bind("<<TreeviewSelect>>", self.on_case_tree_click)
        self.case_tree.bind("<Double-1>", lambda e: self.open_selected_case())

        tree_scroll = ttk.Scrollbar(self.explorer_frame, command=self.case_tree.yview)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.case_tree.config(yscrollcommand=tree_scroll.set)

        # COLUMN 3: Brief panel
        insights_frame = ttk.LabelFrame(parent,
                                        text=" Verbatim Ruling Insights & Case Brief ",
                                        padding=8)
        insights_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # Brief toolbar
        brief_toolbar = ttk.Frame(insights_frame)
        brief_toolbar.pack(side=tk.TOP, fill=tk.X, pady=(0, 4))
        ttk.Button(
            brief_toolbar, text="📋 Copy Brief",
            command=self._copy_case_brief,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            brief_toolbar, text="📄 Open Full Text",
            command=self.open_selected_case,
        ).pack(side=tk.LEFT, padx=(0, 4))
        self._brief_status_label = ttk.Label(
            brief_toolbar, text="", foreground="#2ecc71", font=("Segoe UI", 9)
        )
        self._brief_status_label.pack(side=tk.LEFT, padx=8)

        brief_text_row = ttk.Frame(insights_frame)
        brief_text_row.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.insights_text = self._make_text(brief_text_row, wrap=tk.WORD,
                                             font=("Segoe UI", 11))
        self.insights_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.insights_text.insert(tk.END,
            "Select a case from the Explorer matrix to view its brief.")

        ins_scroll = ttk.Scrollbar(brief_text_row, command=self.insights_text.yview)
        ins_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.insights_text.config(yscrollcommand=ins_scroll.set)

    def _build_ai_assistant_tab(self, parent):
        """Full LLM-powered search / ask / analyse panel."""
        p = self._pal

        # ── Left sidebar: mode + quick actions ───────────────────────
        sidebar = ttk.LabelFrame(parent, text=" Actions ", padding=8)
        sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))

        ttk.Label(sidebar, text="Ask / Search", font=("Segoe UI", 9, "bold")).pack(
            anchor=tk.W, pady=(0, 4))

        ttk.Button(sidebar, text="🔍 Search Archive",
                   style="Accent.TButton",
                   command=self._ai_search).pack(fill=tk.X, pady=2)
        ttk.Button(sidebar, text="❓ Ask a Question",
                   command=self._ai_ask).pack(fill=tk.X, pady=2)
        ttk.Button(sidebar, text="📑 Semantic Search",
                   command=self._ai_semantic).pack(fill=tk.X, pady=2)

        ttk.Separator(sidebar, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)
        ttk.Label(sidebar, text="Analyse Selected Case",
                  font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, pady=(0, 4))

        for label, instr in [
            ("📋 Summarise",        "Provide a concise summary of the case: key facts, issue, holding, and reasoning."),
            ("⚖️ Key Issues",       "List and explain every major legal issue raised and how the court resolved each."),
            ("🔍 Find Weaknesses",  "Identify the weakest arguments in the majority opinion and any notable dissents."),
            ("🔗 Qualified Immunity","Analyse this case in the context of the qualified immunity doctrine."),
            ("📝 IRAC Outline",     "Produce a full IRAC outline (Issue, Rule, Application, Conclusion) for this case."),
        ]:
            btn = ttk.Button(sidebar, text=label,
                             command=lambda i=instr: self._ai_analyse(i))
            btn.pack(fill=tk.X, pady=2)

        ttk.Separator(sidebar, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)
        ttk.Button(sidebar, text="🛑 Stop",
                   command=self._ai_stop).pack(fill=tk.X, pady=2)
        ttk.Button(sidebar, text="🗑 Clear Output",
                   command=self._ai_clear_output).pack(fill=tk.X, pady=2)

        # ── Right panel: prompt + output ──────────────────────────────
        right = ttk.Frame(parent, padding=0)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Prompt row
        prompt_frame = ttk.LabelFrame(right, text=" Prompt ", padding=6)
        prompt_frame.pack(fill=tk.X, pady=(0, 6))

        self.ai_query_text = self._make_text(prompt_frame, height=3, wrap=tk.WORD)
        self.ai_query_text.pack(fill=tk.X, expand=True, side=tk.LEFT)
        self.ai_query_text.insert(tk.END, "Type your question or instruction here…")
        self.ai_query_text.bind("<FocusIn>",  self._ai_prompt_focus_in)
        self.ai_query_text.bind("<FocusOut>", self._ai_prompt_focus_out)

        run_btn = ttk.Button(prompt_frame, text="▶ Run",
                             style="Accent.TButton",
                             command=self._ai_run_custom)
        run_btn.pack(side=tk.RIGHT, padx=(8, 0))

        # Status bar
        self._ai_status = tk.StringVar(value="Ready — configure LLM_API_KEY env var and llm.model in config.yaml")
        status_bar = ttk.Label(right, textvariable=self._ai_status, style="Dim.TLabel")
        status_bar.pack(anchor=tk.W, pady=(0, 4))

        # Output pane
        out_frame = ttk.LabelFrame(right, text=" Output ", padding=6)
        out_frame.pack(fill=tk.BOTH, expand=True)

        self.ai_output = self._make_text(out_frame, wrap=tk.WORD, font=("Segoe UI", 11))
        self.ai_output.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.ai_output.tag_config("heading",  foreground=p["ACCENT"],
                                  font=("Segoe UI", 11, "bold"))
        self.ai_output.tag_config("source",   foreground=p["ACCENT2"],
                                  font=("Segoe UI", 10))
        self.ai_output.tag_config("dim",      foreground=p["FG_DIM"],
                                  font=("Segoe UI", 9, "italic"))
        self.ai_output.tag_config("error",    foreground="#ff5252",
                                  font=("Segoe UI", 10, "bold"))
        self.ai_output.tag_config("body",     foreground=p["FG"])
        self.ai_output.insert(tk.END,
            "← Use the action buttons or type a custom prompt and press ▶ Run.\n\n"
            "Tip: select a case in the Archive Explorer tab first, then click any "
            "'Analyse Selected Case' button to interrogate it directly.\n",
            "dim")

        out_scroll = ttk.Scrollbar(out_frame, command=self.ai_output.yview)
        out_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.ai_output.config(yscrollcommand=out_scroll.set)

        # Copy-output button row
        copy_row = ttk.Frame(right, padding=(0, 4, 0, 0))
        copy_row.pack(fill=tk.X)
        ttk.Button(copy_row, text="📋 Copy Output",
                   command=self._ai_copy_output).pack(side=tk.LEFT)
        self._ai_busy = False

    # -----------------------------------------------------------------
    # AI ASSISTANT — prompt helpers
    # -----------------------------------------------------------------

    _AI_PLACEHOLDER = "Type your question or instruction here…"

    def _ai_prompt_focus_in(self, _event=None):
        if self.ai_query_text.get("1.0", tk.END).strip() == self._AI_PLACEHOLDER:
            self.ai_query_text.delete("1.0", tk.END)

    def _ai_prompt_focus_out(self, _event=None):
        if not self.ai_query_text.get("1.0", tk.END).strip():
            self.ai_query_text.insert("1.0", self._AI_PLACEHOLDER)

    def _ai_get_prompt(self) -> str:
        txt = self.ai_query_text.get("1.0", tk.END).strip()
        return "" if txt == self._AI_PLACEHOLDER else txt

    def _ai_set_status(self, msg: str):
        self.root.after(0, self._ai_status.set, msg)

    def _ai_append(self, text: str, tag: str = "body"):
        """Thread-safe append to the output pane."""
        def _do():
            self.ai_output.config(state=tk.NORMAL)
            self.ai_output.insert(tk.END, text, tag)
            self.ai_output.see(tk.END)
            self.ai_output.config(state=tk.DISABLED)
        self.root.after(0, _do)

    def _ai_clear_output(self):
        self.ai_output.config(state=tk.NORMAL)
        self.ai_output.delete("1.0", tk.END)
        self.ai_output.config(state=tk.DISABLED)
        self._ai_status.set("Output cleared.")

    def _ai_copy_output(self):
        content = self.ai_output.get("1.0", tk.END).strip()
        if content:
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self._ai_status.set("Output copied to clipboard.")

    def _ai_stop(self):
        self._ai_busy = False
        self._ai_set_status("Stopped.")

    def _ai_guard(self) -> bool:
        """Return True if we can start a new request; set status and return False if busy."""
        if self._ai_busy:
            self._ai_set_status("⏳ A request is already running. Wait or press Stop.")
            return False
        if not self.db_path or not os.path.exists(self.db_path):
            self._ai_set_status("❌ Database not found. Ingest cases first.")
            return False
        return True

    # -----------------------------------------------------------------
    # AI ASSISTANT — action handlers
    # -----------------------------------------------------------------

    def _ai_ask(self):
        prompt = self._ai_get_prompt()
        if not prompt:
            self._ai_set_status("Enter a question in the prompt box first.")
            return
        if not self._ai_guard():
            return
        self._ai_busy = True
        self._ai_set_status("⏳ Querying LLM…")
        threading.Thread(target=self._ai_worker_ask, args=(prompt,), daemon=True).start()

    def _ai_search(self):
        prompt = self._ai_get_prompt()
        if not prompt:
            self._ai_set_status("Enter search terms in the prompt box first.")
            return
        if not self._ai_guard():
            return
        self._ai_busy = True
        self._ai_set_status("⏳ Searching archive…")
        threading.Thread(target=self._ai_worker_semantic, args=(prompt,), daemon=True).start()

    def _ai_semantic(self):
        prompt = self._ai_get_prompt()
        if not prompt:
            self._ai_set_status("Enter a query in the prompt box first.")
            return
        if not self._ai_guard():
            return
        self._ai_busy = True
        self._ai_set_status("⏳ Running semantic search…")
        threading.Thread(target=self._ai_worker_semantic, args=(prompt,), daemon=True).start()

    def _ai_analyse(self, instruction: str):
        doc_id = self.get_selected_doc_id()
        if not doc_id:
            self._ai_set_status("Select a case in the Archive Explorer tab first.")
            return
        if not self._ai_guard():
            return
        self._ai_busy = True
        self._ai_set_status(f"⏳ Analysing case {doc_id[:12]}…")
        threading.Thread(target=self._ai_worker_analyse,
                         args=(doc_id, instruction), daemon=True).start()

    def _ai_run_custom(self):
        prompt = self._ai_get_prompt()
        if not prompt:
            self._ai_set_status("Enter a prompt first.")
            return
        doc_id = self.get_selected_doc_id()
        if doc_id:
            # Has a selected case → treat as case analysis with the custom instruction
            if not self._ai_guard():
                return
            self._ai_busy = True
            self._ai_set_status(f"⏳ Running custom prompt on case {doc_id[:12]}…")
            threading.Thread(target=self._ai_worker_analyse,
                             args=(doc_id, prompt), daemon=True).start()
        else:
            # No case selected → treat as archive question
            if not self._ai_guard():
                return
            self._ai_busy = True
            self._ai_set_status("⏳ Running custom prompt against archive…")
            threading.Thread(target=self._ai_worker_ask,
                             args=(prompt,), daemon=True).start()

    # -----------------------------------------------------------------
    # AI ASSISTANT — background workers
    # -----------------------------------------------------------------

    def _ai_worker_ask(self, question: str):
        try:
            answer, sources = query_cases(self.db_path, question)
            self._ai_append(f"\n{'━'*60}\n", "dim")
            self._ai_append(f"❓ {question}\n\n", "heading")
            self._ai_append(answer + "\n", "body")
            if sources:
                self._ai_append("\n── Sources ──────────────────────────────────\n", "dim")
                for s in sources:
                    label = s.get("ref_no") or (s["doc_id"][:12] + "…")
                    folder = s.get("virtual_folder") or "Uncategorized"
                    url = s.get("source_url") or ""
                    line = f"  • {label}  |  {folder}"
                    if url:
                        line += f"\n    {url}"
                    self._ai_append(line + "\n", "source")
            self._ai_set_status(f"✅ Done — {len(sources)} source(s) cited.")
        except Exception as exc:
            self._ai_append(f"\n❌ Error: {exc}\n", "error")
            self._ai_set_status(f"❌ Error: {exc}")
        finally:
            self._ai_busy = False

    def _ai_worker_semantic(self, query: str):
        try:
            results = semantic_search(self.db_path, query)
            self._ai_append(f"\n{'━'*60}\n", "dim")
            self._ai_append(f"🔍 Semantic Search: {query}\n\n", "heading")
            if not results:
                self._ai_append("No relevant cases found.\n", "body")
            else:
                for i, r in enumerate(results, 1):
                    label = r.get("ref_no") or (r["doc_id"][:12] + "…")
                    folder = r.get("virtual_folder") or "Uncategorized"
                    note = r.get("relevance_note", "")
                    snippet = r.get("snippet", "")
                    self._ai_append(f"[{i}] {label}  —  {folder}\n", "source")
                    if note:
                        self._ai_append(f"    {note}\n", "dim")
                    if snippet:
                        self._ai_append(f'    "{snippet[:200]}…"\n', "body")
                    self._ai_append("\n", "body")
            self._ai_set_status(f"✅ Done — {len(results)} result(s).")
        except Exception as exc:
            self._ai_append(f"\n❌ Error: {exc}\n", "error")
            self._ai_set_status(f"❌ Error: {exc}")
        finally:
            self._ai_busy = False

    def _ai_worker_analyse(self, doc_id: str, instruction: str):
        try:
            result = analyze_case(self.db_path, doc_id, instruction)
            self._ai_append(f"\n{'━'*60}\n", "dim")
            self._ai_append(f"⚖️ Case: {doc_id[:12]}…\n", "heading")
            self._ai_append(f"📋 {instruction}\n\n", "dim")
            self._ai_append(result + "\n", "body")
            self._ai_set_status("✅ Analysis complete.")
        except Exception as exc:
            self._ai_append(f"\n❌ Error: {exc}\n", "error")
            self._ai_set_status(f"❌ Error: {exc}")
        finally:
            self._ai_busy = False

        
    def abort_ai_execution(self):
        """Signals active loops to shut down and forcibly restores button states instantly."""
        self.stop_event.set()
        self.ai_status_var.set("Status: Process Stopped")
        
        self.btn_ai_train.config(state=tk.NORMAL)
        self.btn_ai_generate.config(state=tk.NORMAL)
        
        self.log_to_live_engine("🛑 Interrupt sequence fired. Engine shutting down...")

    def log_to_live_engine(self, text_string: str):
        """Append a message to the live engine console panel (thread-safe)."""
        def append_action():
            if "❌" in text_string or "Error" in text_string:
                console_tag = "error"
            elif "⚠️" in text_string or "Warning" in text_string:
                console_tag = "warning"
            elif "✅" in text_string or "💾" in text_string:
                console_tag = "success"
            elif "🛑" in text_string:
                console_tag = "system"
            else:
                console_tag = "info"
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, f"\n[Engine] {text_string}", console_tag)
            if self.log_autoscroll_var.get():
                self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.root.after(0, append_action)

    def trigger_ai_inference(self):
        """Prepares state counters and kicks off async token derivation."""
        prompt_text = self.ai_prompt_entry.get().strip()
        if not prompt_text:
            return
            
        self.stop_event.clear()
        self.ai_status_var.set("Status: Generating tokens...")
        self.btn_ai_generate.config(state=tk.DISABLED)
        
        threading.Thread(target=self._async_ai_generation_worker, args=(prompt_text,), daemon=True).start()

    def _async_ai_generation_worker(self, prompt_text):
        """RAG-first response worker with local LLM fallback."""
        try:
            selected_doc_id = self.get_selected_doc_id()
            ai_response = generate_study_response(
                db_path=self.db_path,
                prompt=prompt_text,
                selected_doc_id=selected_doc_id,
                max_sources=4,
            )
            if (not ai_response.strip()) or ai_response in {NO_DOCS_SENTINEL, NO_MATCH_SENTINEL}:
                import infer
                ai_response = infer.generate(prompt=prompt_text)
            self.root.after(0, self._update_ui_with_ai_text, ai_response)
        except Exception as err:
            self.root.after(0, self.ai_status_var.set, "Error: Generation failed.")
            print(f"[AI Engine Error] Inference crash: {err}")
        finally:
            self.root.after(0, self.btn_ai_generate.config, {"state": tk.NORMAL})

    def _update_ui_with_ai_text(self, text_output):
        """Appends generated AI insights safely onto the end of your main text viewer area."""
        viewer = getattr(self, 'log_text', None)
        if viewer:
            viewer.config(state=tk.NORMAL)
            viewer.insert(tk.END, f"\n\n==================================================\n")
            viewer.insert(tk.END, f"🤖 LOCAL LLM ANALYSIS\n")
            viewer.insert(tk.END, f"==================================================\n")
            viewer.insert(tk.END, f"{text_output}\n")
            viewer.config(state=tk.DISABLED)
            viewer.see(tk.END)
        self.ai_status_var.set("Status: Generation complete.")

    # =====================================================================
    # 🔌 CLOUD TRAINING CONNECTION (Ngrok + Colab T4 Gateway)
    # =====================================================================
    def trigger_ai_training(self):
        """Disables the Train button and safely starts the Colab worker thread."""
        self.btn_ai_train.config(state=tk.DISABLED)
        self.stop_event.clear()
        self.ai_status_var.set("Status: Connecting to Cloud...")
        
        threading.Thread(target=self._async_ai_training_worker, daemon=True).start()

    def _async_ai_training_worker(self):
        """Streams the entire database (texts AND labels) to the Cloud GPU using dynamic batches."""
        import requests
        import sqlite3
        import time
        import os
        
        # --- YOUR RESERVED NGROK DOMAIN ---
        COLAB_URL = "https://enlighten-delouse-refinance.ngrok-free.dev" 
        # ----------------------------------
        
        MAX_SINGLE_DOC_CHARS = 50000  
        MAX_BATCH_CHARS = 1000000     
        
        try:
            # 1. Query text AND labels from the database
            self.log_to_live_engine("📂 Querying local database records and labels...")
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # ⚠️ NOTE: If your category column is named something else, change 'category_id' below!
            cursor.execute("SELECT text, category_id FROM documents WHERE text IS NOT NULL AND text != ''")
            raw_rows = cursor.fetchall()
            conn.close()
            
            total_records = len(raw_rows)
            if total_records == 0:
                self.log_to_live_engine("❌ Aborted: Target database table contains no text.")
                return
                
            # 2. Dynamic Size-Based Chunking (Pairs text with its real label)
            self.log_to_live_engine("🛡️ Sanitizing and packing dataset into safe ~1MB payloads...")
            chunks = []
            current_text_chunk = []
            current_label_chunk = []
            current_char_count = 0
            
            for row in raw_rows:
                doc_text = row[0].strip()
                # Convert label to integer (fallback to 0 if something is blank/corrupted)
                try:
                    doc_label = int(row[1]) 
                except (ValueError, TypeError):
                    doc_label = 0 
                
                if len(doc_text) > MAX_SINGLE_DOC_CHARS:
                    doc_text = doc_text[:MAX_SINGLE_DOC_CHARS] + "\n[TRUNCATED BY ENGINE FOR SIZE]"
                
                doc_len = len(doc_text)
                if current_char_count + doc_len > MAX_BATCH_CHARS and current_text_chunk:
                    # Save both lists in the chunk dictionary
                    chunks.append({"texts": current_text_chunk, "labels": current_label_chunk})
                    current_text_chunk = []
                    current_label_chunk = []
                    current_char_count = 0
                
                current_text_chunk.append(doc_text)
                current_label_chunk.append(doc_label)
                current_char_count += doc_len
                
            if current_text_chunk:
                chunks.append({"texts": current_text_chunk, "labels": current_label_chunk})
                
            total_chunks = len(chunks)
            self.log_to_live_engine(f"📦 Packed {total_records} documents into {total_chunks} dynamic batches.")
            
            # 3. Reset the cloud server's accumulation buffer
            self.log_to_live_engine("🧹 Resetting cloud server dataset buffer...")
            reset_resp = requests.post(f"{COLAB_URL}/reset", timeout=15)
            if reset_resp.status_code != 200:
                self.log_to_live_engine(f"❌ Server connection failed at reset (Status {reset_resp.status_code}). Is Colab updated?")
                return
            
            # 4. Stream chunks sequentially (Now pushing BOTH text and labels)
            for idx, chunk_data in enumerate(chunks, 1):
                if self.stop_event.is_set():
                    self.log_to_live_engine("🛑 Upload process cancelled by user.")
                    return
                
                chunk_bytes = sum(len(d) for d in chunk_data["texts"]) / (1024 * 1024)
                self.log_to_live_engine(f"🚀 Uploading batch {idx} of {total_chunks} ({len(chunk_data['texts'])} cases, {chunk_bytes:.2f} MB)...")
                
                # Send the dictionary containing "texts" and "labels" lists
                response = requests.post(
                    f"{COLAB_URL}/upload_chunk", 
                    json=chunk_data, 
                    timeout=30
                )
                
                if response.status_code != 200:
                    self.log_to_live_engine(f"❌ Batch {idx} upload failed with status {response.status_code}. Aborting.")
                    return
            
            # 5. Trigger training
            self.log_to_live_engine("✅ All dynamic chunks uploaded successfully! Spawning GPU Training Loop...")
            response = requests.post(f"{COLAB_URL}/train", json={}, timeout=15)
            if response.status_code != 200:
                self.log_to_live_engine("❌ Failed to initiate cloud training process.")
                return
            
            self.root.after(0, lambda: self.ai_status_var.set("Status: Training on Cloud T4"))
            
            # 6. Polling loop
            log_offset = 0
            while True:
                time.sleep(1.0)
                
                if self.stop_event.is_set():
                    self.log_to_live_engine("🛰️ Transmitting stop signal to cloud server...")
                    requests.post(f"{COLAB_URL}/stop")
                    self.stop_event.clear()
                
                status_res = requests.get(f"{COLAB_URL}/status", params={"offset": log_offset}, timeout=5)
                if status_res.status_code == 200:
                    data = status_res.json()
                    new_logs = data.get("logs", [])
                    is_training = data.get("is_training", False)
                    
                    for msg in new_logs:
                        self.log_to_live_engine(f"[Colab T4] {msg}")
                    
                    log_offset += len(new_logs)
                    
                    if not is_training:
                        break
                else:
                    self.log_to_live_engine("⚠️ Connection warning: Waiting for Colab to respond...")
            
            # 7. Download weights
            self.log_to_live_engine("📥 Fetching finished model weights from cloud server...")
            dl_res = requests.get(f"{COLAB_URL}/download", stream=True, timeout=30)
            
            if dl_res.status_code == 200:
                local_model_path = os.path.join(os.path.dirname(__file__), "src", "llm", "model.pt")
                os.makedirs(os.path.dirname(local_model_path), exist_ok=True)
                
                with open(local_model_path, "wb") as f:
                    for chunk in dl_res.iter_content(chunk_size=8192):
                        f.write(chunk)
                self.log_to_live_engine("💾 Cloud model synced and saved locally to: src/llm/model.pt")
            else:
                self.log_to_live_engine("⚠️ Training finished, but no new model file was retrieved.")
                
        except Exception as err:
            self.log_to_live_engine(f"❌ Network Thread Error: {err}")
        finally:
            self.root.after(0, lambda: self.btn_ai_train.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.ai_status_var.set("Status: Engine Idle"))

    def create_api_toggle_ui(self, parent_frame, cfg):
        """Generates a list of toggle switches for each API key."""
        initialize_token_registry(cfg)
        tk.Label(parent_frame, text="API Token Management", font=("Arial", 10, "bold")).pack(anchor="w", pady=(10, 5))
    
        for token, meta in TOKEN_REGISTRY.items():
            masked_sig = f"...{token[-6:]}" if len(token) > 6 else token
            var = tk.BooleanVar(value=meta["enabled"])
            
            def on_toggle(t=token, v=var):
                TOKEN_REGISTRY[t]["enabled"] = v.get()
                status = "ENABLED" if v.get() else "DISABLED"
                self.log_to_live_engine(f"[UI] Token ending in {t[-6:]} manually {status}.")
            
            cb = tk.Checkbutton(
                parent_frame, 
                text=f"Key: {masked_sig}", 
                variable=var, 
                command=on_toggle
            )
            cb.pack(anchor="w", padx=10)

    def switch_engine(self):
        selected = self.active_engine.get()
        if selected == "BULK":
            self.write_to_engine_log("[SYSTEM] Switched to BULK S3 Streaming Mode. Disengaging API keys...\n")
        else:
            self.write_to_engine_log("[SYSTEM] Switched to Live API Mode. Restoring priority queue...\n")
        
        if self.crawler_process:
            self.write_to_engine_log("[SYSTEM] Hot-swapping background engines, please hold...\n")
            self.stop_crawler()
            self.root.after(500, self.start_crawler)

    def execute_token_toggle(self, token, variable):
        """Callback engine fired whenever an app switch is flipped."""
        is_checked = variable.get()
        TOKEN_REGISTRY[token]["enabled"] = is_checked
        status = "ONLINE" if is_checked else "OFFLINE"
        self.write_to_engine_log(f"[SYSTEM] Token ending in ...{token[-6:]} manually toggled {status}.\n")

    def _create_desktop_shortcut(self):
        """Runs create_shortcut.py to install a desktop icon for LegalSorter."""
        shortcut_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "create_shortcut.py")
        if not os.path.exists(shortcut_script):
            messagebox.showerror("Create Shortcut", "create_shortcut.py not found in the application directory.")
            return
        try:
            result = subprocess.run(
                [sys.executable, shortcut_script],
                capture_output=True, text=True, timeout=15,
            )
            output = (result.stdout + result.stderr).strip()
            if result.returncode == 0:
                messagebox.showinfo("Create Desktop Shortcut", f"Shortcut created successfully!\n\n{output}")
            else:
                messagebox.showerror("Create Desktop Shortcut", f"Script exited with errors:\n\n{output}")
        except Exception as e:
            messagebox.showerror("Create Desktop Shortcut", f"Failed to run shortcut creator: {e}")

    def open_api_key_manager(self):
        """Opens a full-screen-friendly dialog to view and toggle all registered API keys."""
        win = tk.Toplevel(self.root)
        win.title("⚙️ API Key Manager")
        win.geometry("560x440")
        win.resizable(True, True)
        win.grab_set()

        ttk.Label(win, text="API Key Manager", font=("Segoe UI", 13, "bold")).pack(
            anchor="w", padx=16, pady=(14, 4))
        ttk.Label(
            win,
            text="Enable or disable individual API tokens used by the crawler engine.\n"
                 "Changes take effect immediately — no restart required.",
            font=("Segoe UI", 9),
            foreground="gray",
            justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 10))

        ttk.Separator(win, orient="horizontal").pack(fill=tk.X, padx=12, pady=(0, 8))

        scroll_frame_outer = ttk.Frame(win)
        scroll_frame_outer.pack(fill=tk.BOTH, expand=True, padx=12)

        canvas = tk.Canvas(scroll_frame_outer, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(scroll_frame_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = ttk.Frame(canvas)
        canvas_win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(canvas_win, width=canvas.winfo_width())

        inner.bind("<Configure>", _on_configure)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(canvas_win, width=e.width))

        try:
            cfg = cfgmod.load_config()
            initialize_token_registry(cfg)
        except Exception:
            pass

        if not TOKEN_REGISTRY:
            ttk.Label(inner, text="No API tokens found in registry.", foreground="gray").pack(padx=16, pady=20)
        else:
            for token, meta in TOKEN_REGISTRY.items():
                row = ttk.Frame(inner, padding=(8, 4))
                row.pack(fill=tk.X, pady=2)

                masked = f"...{token[-8:]}" if len(token) > 8 else token
                var = tk.BooleanVar(value=meta["enabled"])

                cb = ttk.Checkbutton(
                    row,
                    text=f"Key:  {masked}",
                    variable=var,
                    command=lambda t=token, v=var: self.execute_token_toggle(t, v),
                    width=30,
                )
                cb.pack(side=tk.LEFT)

                status_lbl = ttk.Label(
                    row,
                    text="● ACTIVE" if meta["enabled"] else "○ DISABLED",
                    foreground="#2ecc71" if meta["enabled"] else "#aaaaaa",
                    font=("Segoe UI", 9),
                )
                status_lbl.pack(side=tk.LEFT, padx=8)

                def _make_refresh(v=var, lbl=status_lbl, t=token):
                    def _refresh():
                        TOKEN_REGISTRY[t]["enabled"] = v.get()
                        lbl.config(
                            text="● ACTIVE" if v.get() else "○ DISABLED",
                            foreground="#2ecc71" if v.get() else "#aaaaaa",
                        )
                        status = "ONLINE" if v.get() else "OFFLINE"
                        self.write_to_engine_log(
                            f"[SYSTEM] Token ending in ...{t[-6:]} toggled {status}.\n"
                        )
                    return _refresh

                cb.config(command=_make_refresh())

        ttk.Separator(win, orient="horizontal").pack(fill=tk.X, padx=12, pady=8)

        # ── Add a new API token ───────────────────────────────────────────────
        add_frame = ttk.LabelFrame(win, text=" Add New API Token ")
        add_frame.pack(fill=tk.X, padx=12, pady=(0, 4))

        new_key_var = tk.StringVar()
        new_key_entry = ttk.Entry(add_frame, textvariable=new_key_var, show="*", width=42)
        new_key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 4), pady=8)

        def _toggle_show_key():
            new_key_entry.config(show="" if new_key_entry.cget("show") == "*" else "*")

        ttk.Button(add_frame, text="👁", width=3, command=_toggle_show_key).pack(
            side=tk.LEFT, pady=8
        )

        def _add_key():
            raw = new_key_var.get().strip()
            if len(raw) < 20:
                messagebox.showwarning(
                    "Invalid Token",
                    "That token looks too short. Please paste your full CourtListener API token.",
                    parent=win,
                )
                return
            if raw in TOKEN_REGISTRY:
                messagebox.showinfo("Already Registered", "That token is already in the registry.", parent=win)
                return
            # Register in-memory
            TOKEN_REGISTRY[raw] = {"enabled": True, "cooldown_until": 0, "consecutive_429s": 0}
            # Persist to config.yaml
            try:
                from token_manager import update_config_token
                cfg_all = cfgmod.load_config()
                existing = cfg_all.get("courtlistener", {}).get("api_tokens", [])
                # Append instead of overwriting, by writing directly
                import yaml
                cfg_path = Path(__file__).resolve().parent / "config.yaml"
                cfg_all.setdefault("courtlistener", {})
                tokens = list(existing) + [raw]
                cfg_all["courtlistener"]["api_tokens"] = tokens
                cfg_all["courtlistener"].pop("api_token", None)
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    yaml.safe_dump(cfg_all, fh, default_flow_style=False)
            except Exception as exc:
                self.write_to_engine_log(f"[WARN] Could not persist token to config.yaml: {exc}\n")
            # Add a new row to the scroll list
            masked = f"...{raw[-8:]}" if len(raw) > 8 else raw
            row = ttk.Frame(inner, padding=(8, 4))
            row.pack(fill=tk.X, pady=2)
            var = tk.BooleanVar(value=True)
            status_lbl = ttk.Label(row, text="● ACTIVE", foreground="#2ecc71", font=("Segoe UI", 9))

            def _make_refresh(v=var, lbl=status_lbl, t=raw):
                def _refresh():
                    TOKEN_REGISTRY[t]["enabled"] = v.get()
                    lbl.config(
                        text="● ACTIVE" if v.get() else "○ DISABLED",
                        foreground="#2ecc71" if v.get() else "#aaaaaa",
                    )
                    status = "ONLINE" if v.get() else "OFFLINE"
                    self.write_to_engine_log(f"[SYSTEM] Token ending in ...{t[-6:]} toggled {status}.\n")
                return _refresh

            ttk.Checkbutton(row, text=f"Key:  {masked}", variable=var,
                            command=_make_refresh(), width=30).pack(side=tk.LEFT)
            status_lbl.pack(side=tk.LEFT, padx=8)
            new_key_var.set("")
            self.write_to_engine_log(f"[SYSTEM] New token ending in ...{raw[-6:]} added.\n")
            messagebox.showinfo("Token Added", f"Token ...{raw[-8:]} added and saved to config.yaml.", parent=win)

        ttk.Button(add_frame, text="Add Key", command=_add_key).pack(side=tk.LEFT, padx=(4, 8), pady=8)

        ttk.Separator(win, orient="horizontal").pack(fill=tk.X, padx=12, pady=8)
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 12))

    def import_local_legal_files(self):
        """Copies pre-downloaded .txt legal files from a user-chosen folder into pull_folder
        so the existing watcher pipeline can process them immediately."""
        src_dir = filedialog.askdirectory(
            title="Select folder containing pre-downloaded legal .txt files",
            parent=self.root,
        )
        if not src_dir:
            return

        src_path = Path(src_dir)
        txt_files = list(src_path.glob("*.txt"))
        if not txt_files:
            messagebox.showinfo(
                "No Files Found",
                f"No .txt files were found in:\n{src_dir}",
                parent=self.root,
            )
            return

        try:
            cfg = cfgmod.load_config()
            pull_folder = Path(cfg["pull_folder"])
            pull_folder.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("Configuration Error", f"Could not read pull_folder from config.yaml:\n{exc}", parent=self.root)
            return

        copied = 0
        skipped = 0
        for src_file in txt_files:
            dest = pull_folder / src_file.name
            if dest.exists():
                skipped += 1
                continue
            shutil.copy2(src_file, dest)
            # Copy any existing sidecar as well
            sidecar_src = src_path / (src_file.name + ".meta.json")
            if sidecar_src.exists():
                shutil.copy2(sidecar_src, pull_folder / sidecar_src.name)
            copied += 1

        self.write_to_engine_log(
            f"[IMPORT] Copied {copied} file(s) to pull_folder ({skipped} skipped — already present). "
            f"Start the crawler to process them.\n"
        )
        messagebox.showinfo(
            "Import Complete",
            f"Copied {copied} file(s) into pull_folder.\n"
            f"{skipped} file(s) skipped (already present).\n\n"
            "Press 'Start Crawler' to process the imported files.",
            parent=self.root,
        )

    def sync_database(self):
        """Queries database stats and triggers visual synchronization loops."""
        self.discover_database_path()
        if not os.path.exists(self.db_path):
            self.stat_indexed.config(text="📂 Indexed: DB Not Found")
            return

        try:
            link = sqlite3.connect(self.db_path)
            cursor = link.cursor()

            # PROACTIVE QUEUE DUPLICATION DETECTOR ENGINE
            cursor.execute("""
                UPDATE priority_queue 
                SET status='fetched' 
                WHERE status='pending' AND (
                    citation IN (SELECT id FROM documents) OR
                    citation IN (SELECT DISTINCT SUBSTR(virtual_folder, INSTR(virtual_folder, '/') + 1) FROM documents)
                )
            """)
            if link.total_changes > 0:
                link.commit()

            cursor.execute("SELECT COUNT(*) FROM documents")
            total_indexed = cursor.fetchone()[0]
            self.stat_indexed.config(text=f"📂 Indexed: {total_indexed}")

            size_mb = os.path.getsize(self.db_path) / (1024 * 1024)
            self.stat_size.config(text=f"💾 {size_mb:.2f} MB")

            cursor.execute("SELECT COUNT(*) FROM priority_queue WHERE status='pending'")
            pending_total = cursor.fetchone()[0]
            self.stat_queue.config(text=f"⏳ Queue: {pending_total}")

            self.queue_text.config(state=tk.NORMAL)
            self.queue_text.delete("1.0", tk.END)
            cursor.execute("SELECT citation FROM priority_queue WHERE status='pending' ORDER BY priority_score DESC LIMIT 15")
            rows = cursor.fetchall()
            if not rows:
                self.queue_text.insert(tk.END, "Queue completely empty.\nAll referenced precedent is local!")
            else:
                for row in rows:
                    self.queue_text.insert(tk.END, f"• {row[0]}\n")
            self.queue_text.config(state=tk.DISABLED)

            if total_indexed != self.last_total_indexed:
                self.refresh_case_tree()
                self.last_total_indexed = total_indexed

                link2 = sqlite3.connect(self.db_path)
                latest = link2.execute(
                    "SELECT id FROM documents ORDER BY added_at DESC LIMIT 1"
                ).fetchone()
                link2.close()
                if latest:
                    self.display_case_by_id(latest[0])

            link.close()
        except Exception as error:
            print(f"[Sync Warning] Database sync cycle interrupted: {error}")

    def refresh_case_tree(self):
        """Rebuilds the matrix index using an ultra-strict regex data sanitation sieve."""
        if not os.path.exists(self.db_path):
            return

        # 🛡️ FAILSAFE AUTO-MIGRATION GATEWAY
        try:
            conn = sqlite3.connect(self.db_path)
            for column_name, column_type in [("is_bookmarked", "INTEGER DEFAULT 0"), ("user_folder", "TEXT")]:
                try:
                    conn.execute(f"ALTER TABLE documents ADD COLUMN {column_name} {column_type}")
                except sqlite3.OperationalError:
                    pass
            conn.commit()
            conn.close()
        except Exception as migration_err:
            print(f"[Migration Failsafe] Encountered configuration issue: {migration_err}")

        # Clear existing visual tree nodes
        for item in self.case_tree.get_children():
            self.case_tree.delete(item)

        try:
            link = sqlite3.connect(self.db_path)
            cursor = link.cursor()
            search_query = self.search_var.get().strip()

            if search_query:
                like_term = f"%{search_query}%"
                cursor.execute("""
                    SELECT id, virtual_folder, keywords_json, is_bookmarked, user_folder FROM documents 
                    WHERE id LIKE ? OR text LIKE ? OR virtual_folder LIKE ? OR keywords_json LIKE ?
                    ORDER BY added_at DESC
                """, (like_term, like_term, like_term, like_term))
            else:
                cursor.execute("SELECT id, virtual_folder, keywords_json, is_bookmarked, user_folder FROM documents ORDER BY added_at DESC")
                
            records = cursor.fetchall()
            link.close()

            if not records:
                return

            root_bookmarks = self.case_tree.insert("", "end", text="⭐ My Bookmarks", open=True)
            root_user_folders = self.case_tree.insert("", "end", text="📂 User Folders", open=True)
            root_juris = self.case_tree.insert("", "end", text="🌎 By Jurisdiction", open=False)
            root_years = self.case_tree.insert("", "end", text="📅 By Timeline (Year)", open=False)
            root_types = self.case_tree.insert("", "end", text="⚖️ By Classification (Type)", open=False)
            root_tags = self.case_tree.insert("", "end", text="🏷️ By System Keywords", open=False)

            juris_nodes, year_nodes, type_nodes, tag_nodes, user_nodes = {}, {}, {}, {}, {}
            HTML_TAG_BLACKLIST = {"span", "em", "class", "div", "p", "br", "href", "html", "li", "ul", "style", "text", "citation", "3d", "alia"}
            
            for doc_id, v_folder, keywords_raw, is_bookmarked, user_folder in records:
                folder_clean = (v_folder or "Uncategorized").replace("\\\\", "/").replace("\\", "/")
                parts = [p.strip() for p in folder_clean.split("/") if p.strip()]
                
                jurisdiction = None
                year = None
                law_types = []
                
                for part in parts:
                    part_lower = part.lower()
                    if part.startswith("Jurisdiction_"):
                        jurisdiction = part.replace("Jurisdiction_", "").replace("_", " ").strip()
                    elif part.isdigit() and len(part) == 4 and 1750 <= int(part) <= 2026:
                        year = part
                    else:
                        has_digits = any(c.isdigit() for c in part)
                        if part_lower in HTML_TAG_BLACKLIST or len(part) <= 3 or has_digits:
                            continue
                        law_types.append(part)
                
                try:
                    keywords = json.loads(keywords_raw) if keywords_raw else []
                except Exception:
                    keywords = []

                clean_keywords = []
                for kw in keywords:
                    kw_clean = kw.strip()
                    kw_lower = kw_clean.lower()
                    if not kw_clean or kw_lower.isdigit() or len(kw_clean) <= 3 or kw_lower in HTML_TAG_BLACKLIST:
                        continue
                    if any(noise in kw_lower for noise in ["page", "document", "date", "volume", "section"]):
                        continue
                    if re.search(r'\d{3,}', kw_lower):
                        continue
                    clean_keywords.append(kw_clean)

                base_label = f"Case: {doc_id[:12]}..."
                short_label = f"⭐ {base_label}" if is_bookmarked else f"📄 {base_label}"

                # 1. Populating Custom Curation Nodes
                if is_bookmarked:
                    self.case_tree.insert(root_bookmarks, "end", text=f"⭐ {base_label}", values=(doc_id,))

                if user_folder and user_folder.strip():
                    clean_u_folder = user_folder.strip()
                    if clean_u_folder not in user_nodes:
                        user_nodes[clean_u_folder] = self.case_tree.insert(root_user_folders, "end", text=f"📁 {clean_u_folder}")
                    self.case_tree.insert(user_nodes[clean_u_folder], "end", text=short_label, values=(doc_id,))

                # 2. System Taxonomy Mapping Layer
                juris_key = jurisdiction if jurisdiction else "Unspecified Jurisdiction"
                if juris_key not in juris_nodes:
                    juris_nodes[juris_key] = self.case_tree.insert(root_juris, "end", text=f"📁 {juris_key}")
                self.case_tree.insert(juris_nodes[juris_key], "end", text=short_label, values=(doc_id,))

                year_key = year if year else "Unspecified Year"
                if year_key not in year_nodes:
                    year_nodes[year_key] = self.case_tree.insert(root_years, "end", text=f"📁 {year_key}")
                self.case_tree.insert(year_nodes[year_key], "end", text=short_label, values=(doc_id,))

                if not law_types:
                    law_types = ["General Precedent"]
                for lt in law_types:
                    if lt not in type_nodes:
                        type_nodes[lt] = self.case_tree.insert(root_types, "end", text=f"📁 {lt}")
                    self.case_tree.insert(type_nodes[lt], "end", text=short_label, values=(doc_id,))

                for kw in clean_keywords:
                    if kw not in tag_nodes:
                        tag_nodes[kw] = self.case_tree.insert(root_tags, "end", text=f"🔖 {kw}")
                    self.case_tree.insert(tag_nodes[kw], "end", text=short_label, values=(doc_id,))

            self.sort_tree_children(root_juris)
            self.sort_tree_children(root_years, reverse=True)
            self.sort_tree_children(root_types)
            self.sort_tree_children(root_tags)
            self.sort_tree_children(root_user_folders)

        except Exception as err:
            print(f"[UI Dynamic Index Error] Refresher failure: {err}")

    def sort_tree_children(self, parent, reverse=False):
        """Helper engine component to keep matrix categories pristine and sorted."""
        children = list(self.case_tree.get_children(parent))
        children.sort(key=lambda x: self.case_tree.item(x)["text"].lower(), reverse=reverse)
        for index, child in enumerate(children):
            self.case_tree.move(child, parent, index)

    def on_case_tree_click(self, event):
        """Catches sidebar user selections and commands data renderings."""
        selected_nodes = self.case_tree.selection()
        if not selected_nodes:
            return
        node_data = self.case_tree.item(selected_nodes[0])
        values = node_data.get("values")
        if values:
            target_doc_id = values[0]
            self.display_case_by_id(target_doc_id)

    def display_case_by_id(self, doc_id):
        """Fetches full analytical record and paints the Brief Profile to viewer."""
        if not os.path.exists(self.db_path):
            return
        try:
            link = sqlite3.connect(self.db_path)
            cursor = link.cursor()
            cursor.execute("""
                SELECT id, virtual_folder, entities_json, citations_json, keywords_json, text, source_url 
                FROM documents WHERE id = ?
            """, (doc_id,))
            record = cursor.fetchone()
            link.close()

            if not record:
                return

            doc_id, virtual_folder, entities_raw, citations_raw, keywords_raw, text, source_url = record
            
            try:
                entities = json.loads(entities_raw) if entities_raw else {}
                citations = json.loads(citations_raw) if citations_raw else []
                keywords = json.loads(keywords_raw) if keywords_raw else []
            except Exception:
                entities, citations, keywords = {}, [], []

            ruling_logic = entities.get("RULING_LOGIC", "No explicit ruling keyword extracted.")
            clean_folder = (virtual_folder or "Uncategorized").replace("\\\\", "/").replace("\\", "/")

            # CROSSOVER BLUEPRINT MAP LOCATIONS GENERATION
            blueprint_paths = []
            
            juris_match = "Unspecified Jurisdiction"
            for p in clean_folder.split("/"):
                if p.startswith("Jurisdiction_"):
                    juris_match = p.replace("Jurisdiction_", "").replace("_", " ")
            blueprint_paths.append(f" • 🌎 By Jurisdiction ➔ {juris_match}")
            
            year_match = "Unspecified Year"
            for p in clean_folder.split("/"):
                if p.isdigit() and len(p) == 4 and 1750 <= int(p) <= 2026:
                    year_match = p
            blueprint_paths.append(f" • 📅 By Timeline (Year) ➔ {year_match}")
            
            found_types = False
            for p in clean_folder.split("/"):
                if not p.startswith("Jurisdiction_") and not (p.isdigit() and len(p) == 4):
                    if p.lower() not in {"span", "em", "class", "div", "p", "br", "3d", "alia"} and len(p) > 3 and not any(c.isdigit() for c in p):
                        blueprint_paths.append(f" • ⚖️ By Classification (Type) ➔ {p}")
                        found_types = True
            if not found_types:
                blueprint_paths.append(" • ⚖️ By Classification (Type) ➔ General Precedent")

            for kw in keywords:
                kw_lower = kw.lower().strip()
                if len(kw_lower) > 3 and kw_lower not in {"span", "em", "class", "page", "document", "date"} and not re.search(r'\d{3,}', kw_lower):
                    blueprint_paths.append(f" • 🏷️ By System Keywords ➔ {kw.strip()}")

            brief = []
            brief.append("=========================================================================")
            brief.append(f" CASE BRIEF VIEW PROFILE ")
            brief.append("=========================================================================\n")
            brief.append(f" [CASE ID] {doc_id}")
            brief.append(f" [LIBRARY PATH] Case_Library/{clean_folder}")
            brief.append(f" [SOURCE LINK] {source_url or 'N/A'}\n")
            
            brief.append("-------------------------------------------------------------------------")
            brief.append(" MATRIX CROSSOVER BLUEPRINT MAP LOCATIONS")
            brief.append("-------------------------------------------------------------------------")
            brief.append("\n".join(blueprint_paths) + "\n")

            brief.append("-------------------------------------------------------------------------")
            brief.append(" VERBATIM RULING LOGIC & INSIGHTS")
            brief.append("-------------------------------------------------------------------------")
            brief.append(f"{ruling_logic.strip()}\n")
            brief.append("-------------------------------------------------------------------------")
            brief.append(" DETECTED CROSS-REFERENCE CITATIONS")
            brief.append("-------------------------------------------------------------------------")
            brief.append("\n".join([f" • {c}" for c in citations]) if citations else " None")
            brief.append("\n-------------------------------------------------------------------------")
            brief.append(" RAW TEXT ARCHIVE PREVIEW (FIRST 1500 CHARACTERS)")
            brief.append("-------------------------------------------------------------------------")
            brief.append(f"{text[:1500].strip()}...\n\n[Full text document archived safely in database storage]")

            self.insights_text.config(state=tk.NORMAL)
            self.insights_text.delete("1.0", tk.END)
            self.insights_text.insert(tk.END, "\n".join(brief))
            
        except Exception as err:
            messagebox.showerror("Brief Error", f"Failed fetching case body: {err}")

    def schedule_auto_refresh(self):
        """Automatically synchronizes the UI with the database file every 5 seconds."""
        self.sync_database()
        self.root.after(5000, self.schedule_auto_refresh)

    def update_visual_timer(self):
        """Increments and displays the elapsed running time of the active session."""
        if self.crawler_process and self.session_start_time:
            elapsed = int(time.time() - self.session_start_time)
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)
            self.stat_timer.config(text=f"⏱ {hours:02d}:{minutes:02d}:{seconds:02d}")
            self.root.after(1000, self.update_visual_timer)

    def start_crawler(self):
        """Launches the autonomous legal crawler based on active engine selection."""
        if not self.crawler_process:
            engine_mode = self.active_engine.get()
            base_dir = os.path.dirname(os.path.abspath(__file__))
            
            if engine_mode == "BULK":
                script_path = os.path.join(base_dir, "bulk_ingest.py")
                script_args = [sys.executable, "-u", script_path]
                self.write_to_engine_log(f"[SYSTEM] Spawning engine at target: {script_path}\n")
            else:
                script_path = os.path.join(base_dir, "run.py")
                script_args = [sys.executable, "-u", script_path, "crawl"] 
                self.write_to_engine_log("[SYSTEM] Spawning live API crawler engine...\n")
                
            self.log_session_start_mark = self.log_text.index("end-1c")
            
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            
            self.crawler_process = subprocess.Popen(
                script_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=_subprocess_creationflags(),
            )
            
            self.session_start_time = time.time()
            self.stat_timer.config(foreground="#2ecc71")
            self.update_visual_timer()

            threading.Thread(target=self.stream_engine_output, daemon=True).start()

            self.btn_start_crawler.config(state=tk.DISABLED)
            self.btn_stop_crawler.config(state=tk.NORMAL)

    def stream_engine_output(self):
        """Background thread worker that reads the crawler stdout line by line."""
        while self.crawler_process:
            line = self.crawler_process.stdout.readline()
            if not line:
                break
            text_line = line.decode('utf-8', errors='replace')
            self.root.after(0, self.write_to_engine_log, text_line)

    def write_to_engine_log(self, text):
        """Appends incoming engine terminal prints directly into the dashboard console panel."""
        self.log_text.config(state=tk.NORMAL)
        
        for line in text.splitlines(keepends=True):
            line_upper = line.upper()
            
            if "[ERROR]" in line_upper or "CRITICAL" in line_upper or "FAILED" in line_upper:
                self.log_text.insert(tk.END, line, "error")
            elif "[WARNING]" in line_upper or "[GUARDIAN]" in line_upper or "[STUB]" in line_upper:
                self.log_text.insert(tk.END, line, "warning")
            elif "[INFO]" in line_upper:
                self.log_text.insert(tk.END, line, "info")
            elif "[+]" in line_upper or "DONE:" in line_upper or "SUCCESS" in line_upper:
                self.log_text.insert(tk.END, line, "success")
            elif "[SYSTEM]" in line_upper or "WATCHING" in line_upper or "STANDING BY" in line_upper:
                self.log_text.insert(tk.END, line, "system")
            else:
                self.log_text.insert(tk.END, line)

        if self.log_autoscroll_var.get():
            self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def copy_log_since_start(self):
        """Copies everything printed to the log since the crawler was last started."""
        try:
            content = self.log_text.get(self.log_session_start_mark, "end-1c")
        except tk.TclError:
            content = self.log_text.get("1.0", "end-1c")
   
        if not content.strip():
            self._flash_log_status("Nothing logged since last start.")
            return

        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self._flash_log_status(f"✅ Copied {len(content.splitlines())} lines to clipboard.")

    def copy_full_log(self):
        """Copies the entire engine log to the clipboard."""
        content = self.log_text.get("1.0", "end-1c")
        if not content.strip():
            self._flash_log_status("Log is empty.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self._flash_log_status(f"✅ Full log copied ({len(content.splitlines())} lines).")

    def _flash_log_status(self, msg: str, duration_ms: int = 3000):
        """Temporarily shows a status message in the log toolbar label."""
        try:
            self._log_status_label.config(text=msg)
            self.root.after(duration_ms, lambda: self._log_status_label.config(text=""))
        except Exception:
            pass

    def _clear_log(self):
        """Clears all text from the engine log panel."""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)
        self._flash_log_status("Log cleared.")

    def _copy_case_brief(self):
        """Copies the current case brief panel content to the clipboard."""
        content = self.insights_text.get("1.0", "end-1c").strip()
        if not content or content.startswith("Select a case"):
            try:
                self._brief_status_label.config(text="No brief loaded.")
                self.root.after(3000, lambda: self._brief_status_label.config(text=""))
            except Exception:
                pass
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        try:
            self._brief_status_label.config(text="✅ Brief copied to clipboard.")
            self.root.after(3000, lambda: self._brief_status_label.config(text=""))
        except Exception:
            pass

    def open_database_viewer(self):
        """Lightweight built-in browser for the documents table."""
        if not self.db_path or not os.path.exists(self.db_path):
            messagebox.showwarning("Database Viewer", "Database not found yet.")
            return

        win = tk.Toplevel(self.root)
        win.title("Database Viewer -- documents table")
        win.geometry("1000x700")

        viewer_db_path = self.db_path

        # --- toolbar: filter + toggles + buttons ---
        top = ttk.Frame(win)
        top.pack(fill=tk.X, padx=8, pady=(8, 6))
        ttk.Label(top, text="Filter:").pack(side=tk.LEFT)

        filter_var = tk.StringVar(win)
        filter_entry = ttk.Entry(top, textvariable=filter_var, width=36)
        filter_entry.pack(side=tk.LEFT, padx=(0, 6))

        show_openable_var = tk.BooleanVar(win, value=False)

        refresh_btn = ttk.Button(top, text="Refresh")
        refresh_btn.pack(side=tk.LEFT, padx=(0, 6))
        open_btn = ttk.Button(top, text="Open in App")
        open_btn.pack(side=tk.LEFT, padx=5)

        _debug_btn = ttk.Button(top, text="DEBUG: click test", command=lambda: (print("DEBUG: toolbar clicked"), messagebox.showinfo("DEBUG", "toolbar clicked")))
        _debug_btn.pack(side=tk.RIGHT, padx=6)

        columns = ("id", "ref_no", "virtual_folder", "source_url", "added_at")
        tree = ttk.Treeview(win, columns=columns, show="headings")
        for col, width in zip(columns, (60, 120, 300, 320, 140)):
            tree.heading(col, text=col)
            tree.column(col, width=width, anchor="w")
        tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        vsb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.place(in_=tree, relx=1.0, rely=0, relheight=0.7, anchor="ne")

        # Concept Vector Matches sub-panel
        sim_frame = ttk.LabelFrame(win, text=" Concept Vector Matches (TF-IDF Similarity Matrix) ")
        sim_frame.pack(fill=tk.X, expand=False, padx=8, pady=8)
        sim_label = ttk.Label(
            sim_frame,
            text="Select any row above to instantaneously surface related case concepts.",
            font=("Consolas", 10, "italic"),
            foreground="gray",
        )
        sim_label.pack(padx=12, pady=12, anchor="w")

        def load_rows(*_):
            for item in tree.get_children():
                tree.delete(item)
            try:
                if not viewer_db_path or not os.path.exists(viewer_db_path):
                    messagebox.showerror("Database Viewer", "Database path not found.")
                    return

                conn = sqlite3.connect(viewer_db_path)
                term = filter_var.get().strip()

                base_select = "SELECT id, ref_no, virtual_folder, source_url, source_path, added_at FROM documents"
                where_clauses = []
                params = []

                if term:
                    like = f"%{term}%"
                    where_clauses.append("(ref_no LIKE ? OR virtual_folder LIKE ?)")
                    params.extend([like, like])
                    where_clauses.append("added_at LIKE ?")
                    params.append(like)

                if show_openable_var.get():
                    where_clauses.append("((source_url IS NOT NULL AND source_url != '') OR (source_path IS NOT NULL AND source_path != ''))")

                if where_clauses:
                    query = f"{base_select} WHERE {' AND '.join(where_clauses)} ORDER BY added_at DESC LIMIT 500"
                else:
                    query = f"{base_select} ORDER BY added_at DESC LIMIT 500"

                rows = conn.execute(query, tuple(params)).fetchall()
                conn.close()

                tree["columns"] = ("id", "ref_no", "virtual_folder", "source_url", "added_at")
                tree.heading("id", text="id")
                tree.column("id", width=60, anchor="w")
                tree.heading("ref_no", text="ref_no")
                tree.column("ref_no", width=120, anchor="w")
                tree.heading("virtual_folder", text="virtual_folder")
                tree.column("virtual_folder", width=300, anchor="w")
                tree.heading("source_url", text="source_url")
                tree.column("source_url", width=300, anchor="w")
                tree.heading("added_at", text="added_at")
                tree.column("added_at", width=140, anchor="w")

                for row in rows:
                    display_row = (row[0], row[1], row[2], row[3], row[5])
                    tree.insert("", "end", values=display_row)

            except Exception as e:
                messagebox.showerror("Database Viewer", f"Query failed: {e}")

        def _on_toggle():
            load_rows()

        show_openable_cb = ttk.Checkbutton(
            top,
            text="Show only if openable",
            variable=show_openable_var,
            command=_on_toggle
        )
        show_openable_cb.pack(side=tk.LEFT, padx=(6, 8))

        def open_db_selected_entry():
            try:
                sel = tree.selection()
                if not sel:
                    messagebox.showinfo("Open entry", "No row selected.")
                    return

                item = sel[0]
                vals = tree.item(item, "values") or []
                doc_id = vals[0] if vals else None

                if not doc_id:
                    messagebox.showerror("Open entry", "Selected row has no id.")
                    return

                ok, result = self.open_case_and_repull(doc_id)
                if not ok:
                    messagebox.showerror("Open entry", result)
                else:
                    messagebox.showinfo("Open entry", f"Opened: {result}")

            except Exception as e:
                messagebox.showerror("Open entry", f"Unexpected error: {e}")

        def on_row_select(event):
            try:
                selected_item = tree.selection()
                if not selected_item:
                    return

                row_values = tree.item(selected_item[0], "values") or []
                ref_no = row_values[1] if len(row_values) > 1 else None
                source_url = row_values[3] if len(row_values) > 3 else None

                target_label = ref_no or source_url
                if not target_label or not isinstance(target_label, str):
                    sim_label.config(text="Error: Selected entry has no searchable identifier.", foreground="red")
                    return

                match = None if ref_no else re.search(r'#(\d+)$', source_url or "")
                case_label = ref_no or (f"bulk_{match.group(1)}.txt" if match else target_label)

                try:
                    result = get_similar_cases(case_label, top_n=3)
                except Exception as e:
                    sim_label.config(text=f"Matrix Error: {e}", foreground="#e74c3c")
                    return

                if result.get("error"):
                    sim_label.config(text=f"Matrix Error: {result['error']}", foreground="#e74c3c")
                elif not result.get("matches"):
                    sim_label.config(
                        text=f"Case {case_label} Loaded: No matching concepts discovered in current local index batch.",
                        foreground="#d35400",
                    )
                else:
                    display_lines = [f"Top conceptual legal matches for {case_label}:"]
                    for i, match_data in enumerate(result["matches"]):
                        confidence_percentage = int(match_data.get("score", 0) * 100)
                        match_label = (
                            match_data.get("ref_no")
                            or match_data.get("filename")
                            or match_data.get("label")
                            or "?"
                        )
                        display_lines.append(
                            f" [{i+1}] Match: {match_label} ---> Structural Match Strength: {confidence_percentage}%"
                        )
                    sim_label.config(text="\n".join(display_lines), font=("Consolas", 10, "normal"), foreground="#2c3e50")

            except Exception:
                sim_label.config(text="Matrix Error: unexpected error while computing matches.", foreground="#e74c3c")

        def _wrap(fn, name):
            def _inner(*a, **k):
                try:
                    print(f"DEBUG: calling {name}")
                    return fn(*a, **k)
                except Exception as exc:
                    import traceback
                    traceback.print_exc()
                    messagebox.showerror("DEBUG", f"Callback {name} raised: {exc}")
            return _inner

        refresh_btn.config(command=_wrap(load_rows, "load_rows"))
        open_btn.config(command=_wrap(open_db_selected_entry, "open_db_selected_entry"))

        filter_entry.bind("<Return>", load_rows)
        tree.bind("<<TreeviewSelect>>", on_row_select)
        tree.bind("<Double-1>", lambda e: open_db_selected_entry())

        load_rows()

    def stop_crawler(self):
        """Safely terminates the background crawling process and parks the timer."""
        if self.crawler_process:
            self.crawler_process.terminate()
            self.crawler_process = None
            self.session_start_time = None
            
            self.stat_timer.config(text="⏱ Stopped", foreground="gray")
            self.write_to_engine_log("\n Engine safely halted. Standing by.\n")
            
            self.btn_start_crawler.config(state=tk.NORMAL)
            self.btn_stop_crawler.config(state=tk.DISABLED)
            
            self.write_to_engine_log("[SYSTEM] Re-indexing downloaded cases for Similarity Matrix...\n")
            threading.Thread(target=self._bg_rebuild_similarity_index, daemon=True).start()

    def _bg_rebuild_similarity_index(self):
        """Asynchronous worker executing scikit-learn tasks outside the main window loop."""
        success, message = build_and_cache_index()
        if success:
            self.root.after(0, lambda: self.write_to_engine_log(f"[SUCCESS] {message}\n"))
        else:
            self.root.after(0, lambda: self.write_to_engine_log(f"[WARNING] Similarity Index: {message}\n"))

    def open_selected_case(self):
        """Get selected tree item, extract DB id, and repull/open case."""
        try:
            sel = self.case_tree.selection()
            if not sel:
                messagebox.showinfo("Open case", "No case selected.")
                return
            item = sel[0]
            vals = self.case_tree.item(item, "values")
            doc_id = vals[0] if vals else item

            ok, result = self.open_case_and_repull(doc_id)
            if not ok:
                messagebox.showerror("Open case", result)
            else:
                messagebox.showinfo("Open case", f"Opened temporary copy: {result}")
        except Exception as e:
            messagebox.showerror("Open case", f"Unexpected error: {e}")

    def get_selected_doc_id(self) -> str | None:
        """Helper to safely isolate the active selection's document hash ID."""
        try:
            sel = self.case_tree.selection()
            if not sel:
                return None
            vals = self.case_tree.item(sel[0], "values")
            return vals[0] if vals else None
        except Exception:
            return None

    def toggle_current_bookmark(self):
        """Queries active record state and flips bookmark status value directly."""
        doc_id = self.get_selected_doc_id()
        if not doc_id:
            messagebox.showinfo("Curation Interface", "Select a target case file node inside the library matrix first.")
            return

        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT is_bookmarked FROM documents WHERE id=?", (doc_id,))
            row = cur.fetchone()
            
            if row:
                next_state = 1 if not row[0] else 0
                cur.execute("UPDATE documents SET is_bookmarked=? WHERE id=?", (next_state, doc_id))
                conn.commit()
            conn.close()
            
            self.sync_database()
        except Exception as e:
            messagebox.showerror("Database Action Error", f"Failed to modify bookmark flag: {e}")

    def prompt_user_folder(self):
        """Launches input dialogue to drop cases into user folders."""
        doc_id = self.get_selected_doc_id()
        if not doc_id:
            messagebox.showinfo("Curation Interface", "Select a target case file node inside the library matrix first.")
            return

        import tkinter.simpledialog as sd
        target_folder = sd.askstring("Custom Desk Organization", "Specify custom library folder name:\n(Leave entirely blank to remove sorting properties)")
        
        if target_folder is not None:
            clean_folder_name = target_folder.strip() if target_folder.strip() else None
            try:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()
                cur.execute("UPDATE documents SET user_folder=? WHERE id=?", (clean_folder_name, doc_id))
                conn.commit()
                conn.close()
                
                self.sync_database()
            except Exception as e:
                messagebox.showerror("Database Action Error", f"Failed to assign sorting properties: {e}")

    def toggle_curation_deck(self):
        """Slides open a dedicated line for tools directly above the workspace layout."""
        if self._curation_expanded:
            self.curation_frame.pack_forget()
            self.btn_toggle_curation.config(text="⚙️ Curation Tools ➕")
            self._curation_expanded = False
        else:
            self.curation_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=2, before=self.body_container)
            self.btn_toggle_curation.config(text="⚙️ Curation Tools ➖")
            self._curation_expanded = True

    def start_crawler_action(self):
        """Dynamic bridge that scans for any matching start method variant."""
        for attr in dir(self):
            if attr.startswith("start_") and ("crawl" in attr or "watch" in attr or "engine" in attr or "worker" in attr):
                if attr != "start_crawler_action":
                    getattr(self, attr)()
                    return
        print(f"[Engine Debug] Start method not found. Available 'start' hooks: {[a for a in dir(self) if 'start' in a]}")

    def stop_crawler_action(self):
        """Dynamic bridge that scans for any matching stop method variant."""
        for attr in dir(self):
            if attr.startswith("stop_") and ("crawl" in attr or "watch" in attr or "engine" in attr or "worker" in attr):
                if attr != "stop_crawler_action":
                    getattr(self, attr)()
                    return
        print(f"[Engine Debug] Stop method not found. Available 'stop' hooks: {[a for a in dir(self) if 'stop' in a]}")

    def copy_log_action(self):
        """Dynamic bridge that scans for any matching logging or clipboard copy variant."""
        for attr in dir(self):
            if ("copy" in attr and "log" in attr) or (attr.startswith("copy_") and "log" in attr):
                if attr != "copy_log_action" and callable(getattr(self, attr)):
                    getattr(self, attr)()
                    return
        print(f"[Engine Debug] Copy method not found. Available logging hooks: {[a for a in dir(self) if 'log' in a or 'copy' in a]}")

    def prompt_delete_folder(self):
        """Fetches active user folders, prompts for selection, and clears classification from DB."""
        import tkinter.simpledialog as sd
        import tkinter.messagebox as mb

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT user_folder FROM documents WHERE user_folder IS NOT NULL AND user_folder != ''")
            active_folders = [row[0] for row in cursor.fetchall()]
            conn.close()
        except Exception as err:
            print(f"[Database Error] Failed to scan directory taxonomy: {err}")
            active_folders = []

        if not active_folders:
            mb.showinfo("Delete Folder", "There are currently no active custom folders mapped in the database matrix.")
            return

        folder_manifest = "\n".join([f" • {folder}" for folder in active_folders])
        
        target_folder = sd.askstring(
            "Delete Custom Folder", 
            f"Active Custom Folders:\n{folder_manifest}\n\nEnter the exact name of the folder you want to delete:"
        )

        if not target_folder:
            return
            
        target_folder = target_folder.strip()
        if target_folder not in active_folders:
            mb.showerror("Input Error", f"The folder target '{target_folder}' was not found inside your active taxonomy matrix.")
            return

        confirm = mb.askyesno(
            "Confirm Folder Dissolution", 
            f"Are you sure you want to delete the custom folder group '{target_folder}'?\n\n"
            f"This will remove the folder tracking label from all inner cases. "
            f"The underlying file records themselves will NOT be deleted."
        )

        if confirm:
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("UPDATE documents SET user_folder = NULL WHERE user_folder = ?", (target_folder,))
                conn.commit()
                conn.close()
                
                mb.showinfo("Success", f"Folder tracking profile '{target_folder}' dissolved successfully.")
                self.refresh_case_tree()
            except Exception as execution_err:
                mb.showerror("Database Transaction Error", f"Failed to rewrite structural database layer: {execution_err}")


if __name__ == "__main__":
    root = tk.Tk()
    app = LegalSorterApp(root)
    root.mainloop()
