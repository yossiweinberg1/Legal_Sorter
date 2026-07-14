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
from src.watcher import TOKEN_REGISTRY, initialize_token_registry
from src import config as cfgmod
from similarity_service import build_and_cache_index, get_similar_cases
from src.legal_fetch import CourtListenerClient
import tempfile
import os
import sqlite3
import subprocess
from pathlib import Path
from tkinter import messagebox
from src.legal_fetch import CourtListenerClient
from src import config as cfgmod

class LegalSorterDashboard:
    def __init__(self, root):
        self.root = root
        self.crawler_process = None
        self.session_start_time = None
        self.last_total_indexed = -1 
        self.db_path = None
        self.log_session_start_mark = "1.0"
        self._temp_files = []  # track temp files created by repull so we can clean them up on exit

        # Window Configurations
        self.root.title(" LegalSorter Control Center")
        self.root.geometry("1300x820") 
        
        # Configure Polished Visual Styles
        self.configure_styles()
        
        # Discover and link the live DB file location
        self.discover_database_path()
        
        # Initialize UI Layout and Automation Hooks
        self.build_ui()
        self.schedule_auto_refresh()

        # Intercept window closing to clean up background processes safely
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def make_cl_client(self):
        """
        Build a CourtListenerClient using the first configured token.
        """
        cfg = cfgmod.load_config()
        token = cfg.get("courtlistener", {}).get("api_tokens", [None])[0]
        base = cfg.get("courtlistener", {}).get("base_url", "https://www.courtlistener.com/api/rest/v4")
        return CourtListenerClient(api_token=token, base_url=base)

    def open_case_and_repull(self, doc_id: str):
        """
        Look up source_url/ref_no for doc_id, download to temp via CourtListenerClient,
        open with the OS default viewer, and remember the temp path for cleanup.
        Returns (True, tmp_path) or (False, error_message).
        """
        if not self.db_path:
            return False, "Database path not configured."

        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT source_url, ref_no FROM documents WHERE id=?", (doc_id,))
            row = cur.fetchone()
            conn.close()
        except Exception as e:
            return False, f"DB error: {e}"

        if not row:
            return False, "Document not found in DB."

        source_url, ref_no = row
        if not source_url:
            return False, "No source_url stored for this document."

        client = self.make_cl_client()
        ok, result = client.download_to_temp(source_url, ref_no)
        if not ok:
            return False, result

        tmp_path = result
        try:
            # Open with platform default viewer
            if os.name == "nt":
                os.startfile(tmp_path)
            else:
                if sys.platform == "darwin":
                    subprocess.Popen(["open", tmp_path])
                else:
                    subprocess.Popen(["xdg-open", tmp_path])
        except Exception as e:
            return False, f"Failed to open file: {e}"

        # remember for cleanup
        try:
            if tmp_path not in self._temp_files:
                self._temp_files.append(tmp_path)
        except Exception:
            pass

        return True, tmp_path

    def cleanup_temp_files(self):
        """
        Remove any temp files we created earlier. Call this from on_close.
        """
        for p in list(self._temp_files):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        self._temp_files = []

    def on_close(self):
        """Ensures background threads are fully killed and temp files removed when closing the dashboard window."""
        try:
            # stop crawler or background threads if running
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
            # fallback in case destroy fails
            os._exit(0)


    def configure_styles(self):
        """Injects clean spacing and consistent layout styling rules across widgets."""
        style = ttk.Style()
        style.configure("Treeview", rowheight=24, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    def discover_database_path(self):
        """Resolves live operational database files using project configs and fallbacks."""
        try:
            from src import config as cfgmod
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
 
     # Control buttons (right side)
     self.btn_stop = ttk.Button(top_panel, text=" Stop Crawler", command=self.stop_crawler, state=tk.DISABLED)
     self.btn_stop.pack(side=tk.RIGHT, padx=5)
 
     self.btn_start = ttk.Button(top_panel, text=" Start Crawler", command=self.start_crawler)
     self.btn_start.pack(side=tk.RIGHT, padx=5)
 
     self.btn_sync = ttk.Button(top_panel, text=" Sync Dashboard", command=self.sync_database)
     self.btn_sync.pack(side=tk.RIGHT, padx=5)
 
     self.btn_copy_log = ttk.Button(top_panel, text=" Copy Log", command=self.copy_log_since_start)
     self.btn_copy_log.pack(side=tk.RIGHT, padx=5)
 
     self.btn_view_db = ttk.Button(top_panel, text=" View Database", command=self.open_database_viewer)
     self.btn_view_db.pack(side=tk.RIGHT, padx=5)
 
     # NEW: Open (repull) button
     self.btn_open_repull = ttk.Button(top_panel, text=" Open (repull)", command=self.open_selected_case)
     self.btn_open_repull.pack(side=tk.RIGHT, padx=5)
 
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
 
             # Link Tkinter boolean variable directly to our background state matrix
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
         # Fallback label if config file reading errors out
         ttk.Label(queue_frame, text=f"API Panel Error: {e}", foreground="red").pack(side=tk.BOTTOM)
 
     # Engine State Variable
     self.active_engine = tk.StringVar(value="API")  # Defaults to API
 
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
 
     # IDEA 2: ARCHIVE DEEP SEARCH ENGINE BAR
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
     # Double-click to open selected case (repull)
     self.case_tree.bind("<Double-1>", lambda e: self.open_selected_case())
 
     tree_scroll = ttk.Scrollbar(self.explorer_frame, command=self.case_tree.yview)
     tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
     self.case_tree.config(yscrollcommand=tree_scroll.set)
 
     # COLUMN 3: Ruling Insights Brief Window
     insights_frame = ttk.LabelFrame(split_canvas, text="  Verbatim Ruling Insights & Active Case File Brief ", padding=10)
     insights_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
 
     self.insights_text = tk.Text(insights_frame, wrap=tk.WORD, font=("Segoe UI", 11))
     self.insights_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
     self.insights_text.insert(tk.END, "Select any faceted cross-reference case from the Explorer matrix.")
 
     scrollbar = ttk.Scrollbar(insights_frame, command=self.insights_text.yview)
     scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
     self.insights_text.config(yscrollcommand=scrollbar.set)
 
     # --- Bottom Section (Logs Console Window) ---
     log_frame = ttk.LabelFrame(self.paned_window, text="  Live Run Details & Engine Output ", padding=10)
     self.paned_window.add(log_frame)
 
     self.log_text = tk.Text(log_frame, wrap=tk.WORD, font=("Consolas", 10), height=10, bg="#1e1e1e", fg="#d4d4d4")
     self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
 
     # --- LOG CONSOLE COLOR CODES ---
     self.log_text.tag_config("error", foreground="#ff5252", font=("Consolas", 10, "bold"))      # Neon Red
     self.log_text.tag_config("warning", foreground="#ffb300")                                  # Vibrant Orange/Yellow
     self.log_text.tag_config("info", foreground="#4baffa")                                     # Cyan Blue
     self.log_text.tag_config("success", foreground="#2ecc71", font=("Consolas", 10, "bold"))    # Matrix Green
     self.log_text.tag_config("system", foreground="#888888")                                   # Neutral Gray

     self.log_text.insert(tk.END, "[System Idle] Ready to stream updates.\n")
     self.log_text.config(state=tk.DISABLED)
     self.log_text.insert(tk.END, "[System Idle] Ready to stream updates.\n")
     self.log_text.config(state=tk.DISABLED)
 
     log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
     log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
     self.log_text.config(yscrollcommand=log_scroll.set)
 
 
 
    def create_api_toggle_ui(parent_frame, cfg):
     """Generates a list of toggle switches for each API key."""
     # Ensure the registry is loaded first
     initialize_token_registry(cfg)
    
     tk.Label(parent_frame, text="API Token Management", font=("Arial", 10, "bold")).pack(anchor="w", pady=(10, 5))
    
     for token, meta in TOKEN_REGISTRY.items():
        masked_sig = f"...{token[-6:]}" if len(token) > 6 else token
        
        # Create a Tkinter boolean variable tied to the current registry state
        var = tk.BooleanVar(value=meta["enabled"])
        
        # Callback function that updates the backend registry when clicked
        def on_toggle(t=token, v=var):
            TOKEN_REGISTRY[t]["enabled"] = v.get()
            status = "ENABLED" if v.get() else "DISABLED"
            log.info(f"[UI] Token {t[-6:]} manually {status}.")
        
        # Create the toggle switch (Checkbutton)
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
        
        # Hot-swap mechanism: If crawler is already running, reboot it with the new engine choices
        if self.crawler_process:
            self.write_to_engine_log("[SYSTEM] Hot-swapping background engines, please hold...\n")
            self.stop_crawler()
            # Small brief delay to let old ports clear cleanly
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

            # =========================================================================
            # IDEA 1: PROACTIVE QUEUE DUPLICATION DETECTOR ENGINE
            # =========================================================================
            # Intercepts queue items that match an already downloaded document ID or citation text
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

                # Auto-populate the brief viewer with the newest case
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

        for item in self.case_tree.get_children():
            self.case_tree.delete(item)

        try:
            link = sqlite3.connect(self.db_path)
            cursor = link.cursor()
            search_query = self.search_var.get().strip()

            # IDEA 2: ARCHIVE DEEP SEARCH ENGINE EXECUTION
            if search_query:
                like_term = f"%{search_query}%"
                cursor.execute("""
                    SELECT id, virtual_folder, keywords_json FROM documents 
                    WHERE id LIKE ? OR text LIKE ? OR virtual_folder LIKE ? OR keywords_json LIKE ?
                    ORDER BY added_at DESC
                """, (like_term, like_term, like_term, like_term))
            else:
                cursor.execute("SELECT id, virtual_folder, keywords_json FROM documents ORDER BY added_at DESC")
                
            records = cursor.fetchall()
            link.close()

            if not records:
                return

            # Keep core branches permanently open for scannability
            root_juris = self.case_tree.insert("", "end", text="🌎 By Jurisdiction", open=False)
            root_years = self.case_tree.insert("", "end", text="📅 By Timeline (Year)", open=False)
            root_types = self.case_tree.insert("", "end", text="⚖️ By Classification (Type)", open=False)
            root_tags  = self.case_tree.insert("", "end", text="🏷️ By System Keywords", open=False)

            juris_nodes, year_nodes, type_nodes, tag_nodes = {}, {}, {}, {}

            # Ironclad Visual Noise Blacklists
            HTML_TAG_BLACKLIST = {"span", "em", "class", "div", "p", "br", "href", "html", "li", "ul", "style", "text", "citation", "3d", "alia"}
            
            for doc_id, v_folder, keywords_raw in records:
                folder_clean = (v_folder or "Uncategorized").replace("\\\\", "/").replace("\\", "/")
                parts = [p.strip() for p in folder_clean.split("/") if p.strip()]
                
                jurisdiction = None
                year = None
                law_types = []
                
                # Strict extraction filter for timeline & folder tags
                for part in parts:
                    part_lower = part.lower()
                    if part.startswith("Jurisdiction_"):
                        jurisdiction = part.replace("Jurisdiction_", "").replace("_", " ").strip()
                    elif part.isdigit() and len(part) == 4 and 1750 <= int(part) <= 2026:
                        # Hard constraint on calendar years to crush strings like '5270' or '0615'
                        year = part
                    else:
                        # Block mixed layout text codes, short noise counters, and blacklisted structural variables
                        has_digits = any(c.isdigit() for c in part)
                        if part_lower in HTML_TAG_BLACKLIST or len(part) <= 3 or has_digits:
                            continue
                        law_types.append(part)
                
                # Strict keyword filtering pipeline
                try:
                    keywords = json.loads(keywords_raw) if keywords_raw else []
                except Exception:
                    keywords = []

                clean_keywords = []
                for kw in keywords:
                    kw_clean = kw.strip()
                    kw_lower = kw_clean.lower()
                    
                    # Remove pure numbers, short code values, or layout syntax elements
                    if not kw_clean or kw_lower.isdigit() or len(kw_clean) <= 3 or kw_lower in HTML_TAG_BLACKLIST:
                        continue
                    if any(noise in kw_lower for noise in ["page", "document", "date", "volume", "section"]):
                        continue
                    if re.search(r'\d{3,}', kw_lower):  # Drop strings with clustered serial sequences
                        continue
                        
                    clean_keywords.append(kw_clean)

                short_label = f"📄 Case: {doc_id[:12]}..."

                # A. Jurisdiction Node Mapping
                juris_key = jurisdiction if jurisdiction else "Unspecified Jurisdiction"
                if juris_key not in juris_nodes:
                    juris_nodes[juris_key] = self.case_tree.insert(root_juris, "end", text=f"📁 {juris_key}")
                self.case_tree.insert(juris_nodes[juris_key], "end", text=short_label, values=(doc_id,))

                # B. Timeline Year Node Mapping
                year_key = year if year else "Unspecified Year"
                if year_key not in year_nodes:
                    year_nodes[year_key] = self.case_tree.insert(root_years, "end", text=f"📁 {year_key}")
                self.case_tree.insert(year_nodes[year_key], "end", text=short_label, values=(doc_id,))

                # C. Law Classification Mapping
                if not law_types:
                    law_types = ["General Precedent"]
                for lt in law_types:
                    if lt not in type_nodes:
                        type_nodes[lt] = self.case_tree.insert(root_types, "end", text=f"📁 {lt}")
                    self.case_tree.insert(type_nodes[lt], "end", text=short_label, values=(doc_id,))

                # D. Cleaned System Keywords Mapping
                for kw in clean_keywords:
                    if kw not in tag_nodes:
                        tag_nodes[kw] = self.case_tree.insert(root_tags, "end", text=f"🔖 {kw}")
                    self.case_tree.insert(tag_nodes[kw], "end", text=short_label, values=(doc_id,))

            # Multi-Dimensional Chronological Sorting Layers
            self.sort_tree_children(root_juris)
            self.sort_tree_children(root_years, reverse=True) # Pin recent litigation years to the very top
            self.sort_tree_children(root_types)
            self.sort_tree_children(root_tags)

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

            # =========================================================================
            # IDEA 3: CROSSOVER BLUEPRINT MAP LOCATIONS GENERATION
            # =========================================================================
            # Track down every distinct point of discovery inside our crossover branches
            blueprint_paths = []
            
            # Determine Jurisdiction Branch
            juris_match = "Unspecified Jurisdiction"
            for p in clean_folder.split("/"):
                if p.startswith("Jurisdiction_"):
                    juris_match = p.replace("Jurisdiction_", "").replace("_", " ")
            blueprint_paths.append(f"  • 🌎 By Jurisdiction ➔ {juris_match}")
            
            # Determine Timeline Branch
            year_match = "Unspecified Year"
            for p in clean_folder.split("/"):
                if p.isdigit() and len(p) == 4 and 1750 <= int(p) <= 2026:
                    year_match = p
            blueprint_paths.append(f"  • 📅 By Timeline (Year) ➔ {year_match}")
            
            # Determine Classification Branches
            found_types = False
            for p in clean_folder.split("/"):
                if not p.startswith("Jurisdiction_") and not (p.isdigit() and len(p) == 4):
                    if p.lower() not in {"span", "em", "class", "div", "p", "br", "3d", "alia"} and len(p) > 3 and not any(c.isdigit() for c in p):
                        blueprint_paths.append(f"  • ⚖️ By Classification (Type) ➔ {p}")
                        found_types = True
            if not found_types:
                blueprint_paths.append("  • ⚖️ By Classification (Type) ➔ General Precedent")

            # Append Tag Nodes
            for kw in keywords:
                kw_lower = kw.lower().strip()
                if len(kw_lower) > 3 and kw_lower not in {"span", "em", "class", "page", "document", "date"} and not re.search(r'\d{3,}', kw_lower):
                    blueprint_paths.append(f"  • 🏷️ By System Keywords ➔ {kw.strip()}")

            brief = []
            brief.append("=========================================================================")
            brief.append(f"                        CASE BRIEF VIEW PROFILE                          ")
            brief.append("=========================================================================\n")
            brief.append(f" [CASE ID]         {doc_id}")
            brief.append(f" [LIBRARY PATH]    Case_Library/{clean_folder}")
            brief.append(f" [SOURCE LINK]     {source_url or 'N/A'}\n")
            
            # Render the Blueprint Locations Layout
            brief.append("-------------------------------------------------------------------------")
            brief.append(" MATRIX CROSSOVER BLUEPRINT MAP LOCATIONS (Stored in multiple branches simultaneously)")
            brief.append("-------------------------------------------------------------------------")
            brief.append("\n".join(blueprint_paths) + "\n")

            brief.append("-------------------------------------------------------------------------")
            brief.append(" VERBATIM RULING LOGIC & INSIGHTS")
            brief.append("-------------------------------------------------------------------------")
            brief.append(f"{ruling_logic.strip()}\n")
            brief.append("-------------------------------------------------------------------------")
            brief.append(" DETECTED CROSS-REFERENCE CITATIONS")
            brief.append("-------------------------------------------------------------------------")
            brief.append("\n".join([f"  • {c}" for c in citations]) if citations else "  None")
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
            
            # Dynamically look for the folder where THIS dashboard file lives
            base_dir = os.path.dirname(os.path.abspath(__file__))
            
            if engine_mode == "BULK":
                # Safely resolves to your exact project folder with clean slashes
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
                script_args,  # 💻 Now dynamically switches between scripts!
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            
            self.session_start_time = time.time()
            self.stat_timer.config(foreground="#2ecc71")
            self.update_visual_timer()

            threading.Thread(target=self.stream_engine_output, daemon=True).start()

            self.btn_start.config(state=tk.DISABLED)
            self.btn_stop.config(state=tk.NORMAL)

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
            
            # Apply the color tags you defined based on keywords in the line
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

        import sqlite3
        import re

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

        # create button objects as variables (commands attached after defs)
        refresh_btn = ttk.Button(top, text="Refresh")
        refresh_btn.pack(side=tk.LEFT, padx=(0, 6))
        open_btn = ttk.Button(top, text="Open in App")
        open_btn.pack(side=tk.LEFT, padx=5)

        # debug: visible click test (keep while debugging)
        _debug_btn = ttk.Button(top, text="DEBUG: click test", command=lambda: (print("DEBUG: toolbar clicked"), messagebox.showinfo("DEBUG", "toolbar clicked")))
        _debug_btn.pack(side=tk.RIGHT, padx=6)

        # Tree (create after toolbar so layout is predictable)
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

        # Data loader (respects the toggle and text filter)
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

               # display columns (hide source_path)
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
                  # row layout: (id, ref_no, virtual_folder, source_url, source_path, added_at)
                  display_row = (row[0], row[1], row[2], row[3], row[5])
                  tree.insert("", "end", values=display_row)

           except Exception as e:
               messagebox.showerror("Database Viewer", f"Query failed: {e}")

        # Toggle callback (calls loader)
        def _on_toggle():
           load_rows()

        show_openable_cb = ttk.Checkbutton(
           top,
           text="Show only if openable",
           variable=show_openable_var,
           command=_on_toggle
        )
        show_openable_cb.pack(side=tk.LEFT, padx=(6, 8))

        # Open selected entry in main app (reuses dashboard logic)
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

        # TF-IDF / similarity handler (nested so it can access sim_label/tree)
        def on_row_select(event):
           """Triggers when a user highlights a row in the database view."""
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
                         f"  [{i+1}] Filename: {match_data.get('filename','?')}  ---> Structural Match Strength: {confidence_percentage}%"
                     )
                  sim_label.config(text="\n".join(display_lines), font=("Consolas", 10, "normal"), foreground="#2c3e50")

           except Exception:
               sim_label.config(text="Matrix Error: unexpected error while computing matches.", foreground="#e74c3c")

        # wrapper to catch and show exceptions for button callbacks
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

        # attach wrapped commands after defs
        refresh_btn.config(command=_wrap(load_rows, "load_rows"))
        open_btn.config(command=_wrap(open_db_selected_entry, "open_db_selected_entry"))

        # bindings
        filter_entry.bind("<Return>", load_rows)
        tree.bind("<<TreeviewSelect>>", on_row_select)
        tree.bind("<Double-1>", lambda e: open_db_selected_entry())

        # Initial load
        load_rows()

  
    def stop_crawler(self):
        """Safely terminates the background crawling process and parks the timer."""
        if self.crawler_process:
            self.crawler_process.terminate()
            self.crawler_process = None
            self.session_start_time = None
            
            self.stat_timer.config(text=" Run Time: Stopped", foreground="gray")
            self.write_to_engine_log("\n Engine safely halted. Standing by.\n")
            
            self.btn_start.config(state=tk.NORMAL)
            self.btn_stop.config(state=tk.DISABLED)
            
            # 🌟 INTEGRATION: Rebuild the similarity matrix dynamically when crawling cycles end
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
     """
     Get the selected tree item, extract the DB id, and repull/open the case.
     Adjust extraction if your tree stores a different value.
     """
     try:
         sel = self.case_tree.selection()
         if not sel:
             messagebox.showinfo("Open case", "No case selected.")
             return
         item = sel[0]
 
         # Try to extract DB id from the tree item's values first
         vals = self.case_tree.item(item, "values")
         if vals:
             doc_id = vals[0]
         else:
             # fallback: use the tree item id itself if you used it as the DB id
             doc_id = item

         ok, result = self.open_case_and_repull(doc_id)
         if not ok:
             messagebox.showerror("Open case", result)
         else:
             messagebox.showinfo("Open case", f"Opened temporary copy: {result}")
     except Exception as e:
         messagebox.showerror("Open case", f"Unexpected error: {e}")
   
 
    def open_db_selected_entry(self):
     """
     Open the selected DB viewer row.
     Priority:
       1) If a local source_path exists, open it.
       2) Else if source_url is a valid http(s) URL, repull via CourtListenerClient.
       3) Else show a clear error.
     """
     try:
         self = self.tree.selection()
         if not self:
             messagebox.showinfo("Open entry", "No row selected.")
             return
 
         item = sel[0]
         vals = self.tree.item(item, "values") or []
         doc_id = vals[0] if vals else item
 
         conn = sqlite3.connect(self.db_path)
         cur = conn.cursor()
         cur.execute("""
             SELECT id, ref_no, source_url, source_path
             FROM documents
             WHERE id=? OR ref_no=?
         """, (doc_id, doc_id))
         row = cur.fetchone()
         conn.close()

         if not row:
             messagebox.showerror("Open entry", "Document not found in database.")
             return

         _id, ref_no, source_url, source_path = row

         # 1) Local file exists
         if source_path:
             p = Path(source_path)
             if p.exists():
                 try:
                     if os.name == "nt":
                         os.startfile(str(p))
                     else:
                         subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p)])
                     return
                 except Exception as e:
                     messagebox.showerror("Open entry", f"Failed to open local file: {e}")
                     return

         # 2) Valid URL → repull
         if isinstance(source_url, str) and source_url.lower().startswith(("http://", "https://")):
             cfg = cfgmod.load_config()
             token = cfg.get("courtlistener", {}).get("api_tokens", [None])[0]
             base = cfg.get("courtlistener", {}).get("base_url", "https://www.courtlistener.com/api/rest/v4")
             client = CourtListenerClient(api_token=token, base_url=base)

             ok, tmp_path = client.download_to_temp(source_url, ref_no or f"doc_{_id}")
             if not ok:
                 messagebox.showerror("Open entry", f"Download failed: {tmp_path}")
                 return

             try:
                 if os.name == "nt":
                     os.startfile(tmp_path)
                 else:
                     subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", tmp_path])
                 return
             except Exception as e:
                 messagebox.showerror("Open entry", f"Failed to open downloaded file: {e}")
                 return

         # 3) No usable source
         messagebox.showerror("Open entry", "Selected entry has no valid source_url or local file.")

     except Exception as e:
         messagebox.showerror("Open entry", f"Unexpected error: {e}")



    def make_cl_client_from_config():
     cfg = cfgmod.load_config()
     token = cfg.get("courtlistener", {}).get("api_tokens", [None])[0]
     base = cfg.get("courtlistener", {}).get("base_url", "https://www.courtlistener.com/api/rest/v4")
     return CourtListenerClient(api_token=token, base_url=base)

if __name__ == "__main__":
    app_window = tk.Tk()
    dashboard = LegalSorterDashboard(app_window)
    app_window.mainloop()