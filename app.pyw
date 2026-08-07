import tkinter as tk
from tkinter import ttk, messagebox
import sqlite3
import json
import os
import sys
sys.stdout.reconfigure(encoding='utf-8')
import subprocess
import time
import threading
import re
import tempfile
from pathlib import Path
from src.watcher import TOKEN_REGISTRY, initialize_token_registry
from src import config as cfgmod
from similarity_service import build_and_cache_index, get_similar_cases
from src.legal_fetch import CourtListenerClient
from src.study_assistant import generate_study_response, NO_DOCS_SENTINEL, NO_MATCH_SENTINEL

# Import the window class from the new file you just created
from error_ledger import ErrorLedgerWindow

LEDGER_FILE = "ui_extraction_errors.json"

# =====================================================================
# 🛡️ ANTI-HANG & DIAGNOSTIC BOOTSTRAPPER (Windows ARM64 / PyTorch Safe)
# =====================================================================
print("\n[Bootloader] Initializing system environment...")

# Disable physical CUDA/GPU driver scans to prevent ARM64 driver deadlocks
os.environ["CUDA_VISIBLE_DEVICES"] = ""
# Restrict OpenMP thread allocations during startup initialization
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
print("[Bootloader] Thread and GPU environmental blocks secured.")

# Resolve and append the internal model script path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "src", "llm")))
print("[Bootloader] Source paths appended to environment.")

# Diagnostic pre-import sequence
try:
    print("[Bootloader] Safe-loading PyTorch core...")
    start_t = time.time()
    import torch
    print(f"[Bootloader] PyTorch loaded successfully in {time.time() - start_t:.2f}s.")
    
    print("[Bootloader] Safe-loading training dependencies...")
    start_t = time.time()
    import train
    print(f"[Bootloader] Training matrix loaded successfully in {time.time() - start_t:.2f}s.")
except Exception as e:
    print(f"[Bootloader ❌ ERROR] Pre-import sequence failed: {e}")
    import traceback
    traceback.print_exc()

print("[Bootloader] All systems green. Initializing Tkinter window...\n")

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
        
        # Window Configurations
        self.root.title(" LegalSorter Control Center")
        self.root.geometry("1300x820") 
        
        self.stop_event = threading.Event()
        
        # Configure Polished Visual Styles
        self.configure_styles()
        
        # Discover and link the live DB file location
        self.discover_database_path()
        
        # Initialize UI Layout and Automation Hooks
        self.build_ui()
        self.schedule_auto_refresh()

        # Intercept window closing to clean up background processes safely
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

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

    def open_case_and_repull(self, doc_id: str):
        """
        Unified opener: Checks for a valid CourtListener URL to repull. 
        If bulk data or missing URL, it instantly extracts the text directly from the database.
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

        # FALLBACK: If bulk data or missing URL, reconstruct text file from DB
        if not source_url or source_url.startswith("bulk://"):
            if not text_content:
                return False, "No URL available and no text archived in database."
            
            tmp_path = os.path.join(tempfile.gettempdir(), f"archive_{doc_id[:12]}.txt")
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(text_content)
                
                if tmp_path not in self._temp_files:
                    self._temp_files.append(tmp_path)

                if os.name == "nt":
                    os.startfile(tmp_path)
                else:
                    opener = "open" if sys.platform == "darwin" else "xdg-open"
                    subprocess.Popen([opener, tmp_path])
                
                return True, tmp_path
            except Exception as e:
                return False, f"Failed to generate DB text file: {e}"

        # NORMAL REPULL: If standard HTTP URL exists, use CourtListenerClient
        client = self.make_cl_client()
        ok, result = client.download_to_temp(source_url, ref_no)
        if not ok:
            return False, result

        tmp_path = result
        try:
            if os.name == "nt":
                os.startfile(tmp_path)
            else:
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([opener, tmp_path])
        except Exception as e:
            return False, f"Failed to open file: {e}"

        if tmp_path not in self._temp_files:
            self._temp_files.append(tmp_path)

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
        self.style.configure("Treeview", rowheight=24, font=("Segoe UI", 10))
        self.style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        self.style.configure("Alert.TButton")

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

    def build_ui(self):
        """Assembles the application main control board layout."""
        # Create the physical Alert Button (packed at the absolute top-right)
        self.alert_btn = ttk.Button(
            self.root, 
            text="⚠️ Alerts (0)", 
            command=self.open_ledger,
            style="Alert.TButton"
        )
        self.alert_btn.pack(side=tk.TOP, anchor=tk.E, padx=10, pady=5)
        self.update_alert_button()

        # --- Top Metrics & Control Header Bar ---
        top_panel = ttk.Frame(self.root, padding=10)
        top_panel.pack(side=tk.TOP, fill=tk.X)

        self.stat_indexed = ttk.Label(top_panel, text=" Total Indexed: Calculating...", font=("Segoe UI", 11, "bold"))
        self.stat_indexed.pack(side=tk.LEFT, padx=15)
    
        self.stat_size = ttk.Label(top_panel, text=" Storage Footprint: 0.00 MB", font=("Segoe UI", 11, "bold"))
        self.stat_size.pack(side=tk.LEFT, padx=15)
    
        self.stat_queue = ttk.Label(top_panel, text=" Pending Queue: 0", font=("Segoe UI", 11, "bold"))
        self.stat_queue.pack(side=tk.LEFT, padx=15)
    
        self.stat_timer = ttk.Label(top_panel, text=" Run Time: 00:00:00", font=("Segoe UI", 11, "bold"), foreground="gray")
        self.stat_timer.pack(side=tk.LEFT, padx=15)
    
        # =========================================================================
        # UNIFIED COMMAND LAYOUT DECK (Primary Row)
        # =========================================================================
        self.btn_stop_crawler = ttk.Button(top_panel, text="Stop Crawler", command=self.stop_crawler_action)
        self.btn_stop_crawler.pack(side=tk.RIGHT, padx=6, pady=4)

        self.btn_start_crawler = ttk.Button(top_panel, text="Start Crawler", command=self.start_crawler_action)
        self.btn_start_crawler.pack(side=tk.RIGHT, padx=6, pady=4)

        self.btn_sync = ttk.Button(top_panel, text="Sync Dashboard", command=self.sync_database)
        self.btn_sync.pack(side=tk.RIGHT, padx=6, pady=4)

        self.btn_copy_log = ttk.Button(top_panel, text="Copy Log", command=self.copy_log_action)
        self.btn_copy_log.pack(side=tk.RIGHT, padx=6, pady=4)

        self.btn_view_db = ttk.Button(top_panel, text="View Database", command=self.open_database_viewer)
        self.btn_view_db.pack(side=tk.RIGHT, padx=6, pady=4)

        self.btn_open_repull = ttk.Button(top_panel, text="Open (repull)", command=self.open_selected_case)
        self.btn_open_repull.pack(side=tk.RIGHT, padx=6, pady=4)

        # Master Curation Toggle Button
        self._curation_expanded = False
        self.btn_toggle_curation = ttk.Button(top_panel, text="⚙️ Curation Tools ➕", command=self.toggle_curation_deck)
        self.btn_toggle_curation.pack(side=tk.RIGHT, padx=6, pady=4)

        # =========================================================================
        # DEDICATED SUB-TOOLBAR ROW (Sits right above Verbatim Brief View)
        # =========================================================================
        self.curation_frame = ttk.Frame(self.root)
        
        self.btn_bookmark = ttk.Button(self.curation_frame, text="⭐ Toggle Bookmark", command=self.toggle_current_bookmark)
        self.btn_bookmark.pack(side=tk.LEFT, padx=6, pady=4)

        self.btn_user_folder = ttk.Button(self.curation_frame, text="📁 Set Custom Folder", command=self.prompt_user_folder)
        self.btn_user_folder.pack(side=tk.LEFT, padx=6, pady=4)

        self.btn_delete_folder = ttk.Button(self.curation_frame, text="❌ Delete Folder", command=self.prompt_delete_folder)
        self.btn_delete_folder.pack(side=tk.LEFT, padx=6, pady=4)

        # =========================================================================
        # MAIN BODY LAYOUT CONTAINER 
        # =========================================================================
        self.body_container = ttk.Frame(self.root)
        self.body_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # =========================================================================
        # INTEGRATED LOCAL AI CONSOLE PANEL (Pinned to the Bottom)
        # =========================================================================
        ai_frame = ttk.LabelFrame(self.body_container, text="🤖 Local LLM Insights Engine")
        ai_frame.pack(side=tk.BOTTOM, fill=tk.X, expand=False, padx=5, pady=10)

        # Row 1: Prompt Input Row
        prompt_label = ttk.Label(ai_frame, text="Ask AI about this case:")
        prompt_label.pack(anchor=tk.W, padx=10, pady=(5, 0))
           
        self.ai_prompt_entry = ttk.Entry(ai_frame, font=("Segoe UI", 10))
        self.ai_prompt_entry.pack(fill=tk.X, padx=10, pady=5)
        self.ai_prompt_entry.insert(0, "Summarize the key precedent and rulings of this case.")

        # Row 2: Action Control Deck
        ai_buttons_layout = ttk.Frame(ai_frame)
        ai_buttons_layout.pack(fill=tk.X, padx=10, pady=5)

        self.btn_ai_generate = ttk.Button(ai_buttons_layout, text="✨ Generate Analysis", command=self.trigger_ai_inference)
        self.btn_ai_generate.pack(side=tk.LEFT, padx=2)

        self.btn_ai_train = ttk.Button(ai_buttons_layout, text="⚙️ Train LLM on DB", command=self.trigger_ai_training)
        self.btn_ai_train.pack(side=tk.LEFT, padx=2)

        self.btn_ai_stop = ttk.Button(ai_buttons_layout, text="🛑 Stop AI Process", command=self.abort_ai_execution)
        self.btn_ai_stop.pack(side=tk.LEFT, padx=2)

        # Row 3: Real-Time Engine Thread Status Bar
        self.ai_status_var = tk.StringVar(value="Status: Engine Idle")
        self.ai_status_label = ttk.Label(ai_frame, textvariable=self.ai_status_var, font=("Segoe UI", 9, "italic"), foreground="gray")
        self.ai_status_label.pack(anchor=tk.W, padx=10, pady=(0, 5))

        # --- Main Layout Engine (PanedWindow) ---
        self.paned_window = tk.PanedWindow(self.root, orient=tk.VERTICAL, sashrelief=tk.RAISED)
        self.paned_window.pack(fill=tk.BOTH, expand=True)
    
        # --- Top Section (3-Column Layout: Queue + Tree Explorer + Brief Monitor) ---
        split_canvas = ttk.Frame(self.paned_window, padding=10)
        self.paned_window.add(split_canvas, height=500)
    
        # COLUMN 1: Priority Queue Tracker & API Token Controls
        queue_frame = ttk.LabelFrame(split_canvas, text=" Network Control & Priority Targets ")
        queue_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=False)
    
        # Top Half: Priority Queue Targets Text
        self.queue_text = tk.Text(queue_frame, width=28, height=18, wrap=tk.WORD, font=("Consolas", 10))
        self.queue_text.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(0, 10))
        self.queue_text.insert(tk.END, "Queue empty.")
        self.queue_text.config(state=tk.DISABLED)
    
        # Bottom Half: Dynamically Rendered API Key Switch Toggles
        try:
            cfg = cfgmod.load_config()
            initialize_token_registry(cfg)
    
            api_label_frame = ttk.LabelFrame(queue_frame, text=" Live API Token Toggles ")
            api_label_frame.pack(side=tk.BOTTOM, fill=tk.X, expand=False, ipady=5)
    
            for token, meta in TOKEN_REGISTRY.items():
                masked_sig = f"...{token[-6:]}" if len(token) > 6 else token
                var = tk.BooleanVar(value=meta["enabled"])
    
                def make_toggle_callback(t=token, v=var):
                    return lambda: self.execute_token_toggle(t, v)
    
                cb = tk.Checkbutton(
                    api_label_frame,
                    text=f"Key: {masked_sig}",
                    variable=var,
                    command=make_toggle_callback()
                )
                cb.pack(anchor="w", padx=10, pady=2)
        except Exception as e:
            ttk.Label(queue_frame, text=f"API Panel Error: {e}", foreground="red").pack(side=tk.BOTTOM)
    
        # Engine State Variable
        self.active_engine = tk.StringVar(value="API")
    
        engine_frame = ttk.LabelFrame(queue_frame, text=" Active Ingestion Engine ")
        engine_frame.pack(side=tk.BOTTOM, fill=tk.X, expand=False, ipady=5, pady=(0, 10))
    
        # Radio Toggles
        tk.Radiobutton(
            engine_frame,
            text="API Tracker (Live targeted fetches)",
            variable=self.active_engine,
            value="API",
            command=self.switch_engine
        ).pack(anchor="w", padx=10, pady=2)
    
        tk.Radiobutton(
            engine_frame,
            text="S3 Bulk Dump (High-volume streaming)",
            variable=self.active_engine,
            value="BULK",
            command=self.switch_engine
        ).pack(anchor="w", padx=10, pady=2)
    
        # COLUMN 2: CROSSOVER EXPLORER UPGRADE
        self.explorer_frame = ttk.LabelFrame(split_canvas, text=" Crossover Library Matrix ")
        self.explorer_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=False, padx=10)
    
        # ARCHIVE DEEP SEARCH ENGINE BAR
        search_frame = ttk.Frame(self.explorer_frame)
        search_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)
    
        ttk.Label(search_frame, text="🔍 ").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *args: self.refresh_case_tree())
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=28)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        # Multi-Facet Hierarchical Tree List
        self.case_tree = ttk.Treeview(self.explorer_frame, show="tree", selectmode="browse")
        self.case_tree.column("#0", width=310, minwidth=240)
        self.case_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.case_tree.bind("<<TreeviewSelect>>", self.on_case_tree_click)
        self.case_tree.bind("<Double-1>", lambda e: self.open_selected_case())
    
        tree_scroll = ttk.Scrollbar(self.explorer_frame, command=self.case_tree.yview)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.case_tree.config(yscrollcommand=tree_scroll.set)
    
        # COLUMN 3: Ruling Insights Brief Window
        insights_frame = ttk.LabelFrame(split_canvas, text=" Verbatim Ruling Insights & Active Case File Brief ", padding=10)
        insights_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
    
        self.insights_text = tk.Text(insights_frame, wrap=tk.WORD, font=("Segoe UI", 11))
        self.insights_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.insights_text.insert(tk.END, "Select any faceted cross-reference case from the Explorer matrix.")
    
        scrollbar = ttk.Scrollbar(insights_frame, command=self.insights_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.insights_text.config(yscrollcommand=scrollbar.set)
    
        # --- Bottom Section (Logs Console Window) ---
        log_frame = ttk.LabelFrame(self.paned_window, text=" Live Run Details & Engine Output ", padding=10)
        self.paned_window.add(log_frame)

        self.log_text = tk.Text(log_frame, wrap=tk.WORD, font=("Consolas", 10),
                        height=10, bg="#1e1e1e", fg="#d4d4d4")
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.log_text.bind("<Key>", lambda e: "break")

        # Color tags
        self.log_text.tag_config("error", foreground="#ff5252", font=("Consolas", 10, "bold"))
        self.log_text.tag_config("warning", foreground="#ffb300")
        self.log_text.tag_config("info", foreground="#4baffa")
        self.log_text.tag_config("success", foreground="#2ecc71", font=("Consolas", 10, "bold"))
        self.log_text.tag_config("system", foreground="#888888")

        self.log_text.insert(tk.END, "[System Idle] Ready to stream updates.\n")

        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=log_scroll.set)
        
        def log_to_live_engine(self, text_string):
            def append_action():
                console_tag = "info"
                if "❌" in text_string or "Error" in text_string:
                    console_tag = "error"
                elif "⚠️" in text_string or "Warning" in text_string:
                    console_tag = "warning"
                elif "✅" in text_string or "💾" in text_string:
                    console_tag = "success"
                elif "🛑" in text_string:
                    console_tag = "system"
        
                self.log_text.config(state=tk.NORMAL)
                self.log_text.insert(tk.END, f"\n[AI Engine] {text_string}", console_tag)
                self.log_text.see(tk.END)
                self.log_text.config(state=tk.DISABLED)

            self.root.after(0, append_action)

        
    def abort_ai_execution(self):
        """Signals active loops to shut down and forcibly restores button states instantly."""
        self.stop_event.set()
        self.ai_status_var.set("Status: Process Stopped")
        
        self.btn_ai_train.config(state=tk.NORMAL)
        self.btn_ai_generate.config(state=tk.NORMAL)
        
        self.log_to_live_engine("🛑 Interrupt sequence fired. Engine shutting down...")

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

    def sync_database(self):
        """Queries database stats and triggers visual synchronization loops."""
        self.discover_database_path()
        if not os.path.exists(self.db_path):
            self.stat_indexed.config(text=" Total Indexed: DB Not Found")
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
            self.stat_indexed.config(text=f" Total Indexed: {total_indexed}")

            size_mb = os.path.getsize(self.db_path) / (1024 * 1024)
            self.stat_size.config(text=f" Storage Footprint: {size_mb:.2f} MB")

            cursor.execute("SELECT COUNT(*) FROM priority_queue WHERE status='pending'")
            pending_total = cursor.fetchone()[0]
            self.stat_queue.config(text=f" Pending Queue: {pending_total}")

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
            self.stat_timer.config(text=f" Run Time: {hours:02d}:{minutes:02d}:{seconds:02d}")
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
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
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

    def copy_log_since_start(self):
        """Copies everything printed to the log since the crawler was last started."""
        try:
            content = self.log_text.get(self.log_session_start_mark, "end-1c")
        except tk.TclError:
            content = self.log_text.get("1.0", "end-1c")
   
        if not content.strip():
            messagebox.showinfo("Copy Log", "Nothing has been logged since the last start yet.")
            return

        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        messagebox.showinfo("Copy Log", "Log copied to clipboard.")

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
                source_url = row_values[3] if len(row_values) > 3 else None

                if not source_url or not isinstance(source_url, str):
                    sim_label.config(text="Error: Selected entry has no source URL.", foreground="red")
                    return

                match = re.search(r'#(\d+)$', source_url)
                if not match:
                    sim_label.config(text="Error: Selected entry has an unexpected source URL format.", foreground="red")
                    return

                opinion_id = match.group(1)
                target_filename = f"bulk_{opinion_id}.txt"

                try:
                    result = get_similar_cases(target_filename, top_n=3)
                except Exception as e:
                    sim_label.config(text=f"Matrix Error: {e}", foreground="#e74c3c")
                    return

                if result.get("error"):
                    sim_label.config(text=f"Matrix Error: {result['error']}", foreground="#e74c3c")
                elif not result.get("matches"):
                    sim_label.config(
                        text=f"Case ID #{opinion_id} Loaded: No matching concepts discovered in current local index batch.",
                        foreground="#d35400",
                    )
                else:
                    display_lines = [f"Top conceptual legal matches for Case Target #{opinion_id}:"]
                    for i, match_data in enumerate(result["matches"]):
                        confidence_percentage = int(match_data.get("score", 0) * 100)
                        display_lines.append(
                            f" [{i+1}] Filename: {match_data.get('filename','?')} ---> Structural Match Strength: {confidence_percentage}%"
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
            
            self.stat_timer.config(text=" Run Time: Stopped", foreground="gray")
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
