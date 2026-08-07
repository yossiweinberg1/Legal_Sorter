import json
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

class ErrorLedgerWindow(tk.Toplevel):
    def __init__(self, parent, ledger_path="ui_extraction_errors.json"):
        super().__init__(parent)
        self.parent = parent
        self.ledger_path = Path(ledger_path)
        
        self.title("Ingestion & Extraction Error Ledger")
        self.geometry("900x450")
        self.minsize(700, 300)
        
        # Grab focus to prevent interaction with the main window while open
        self.grab_set()
        
        self._setup_ui()
        self.load_errors()

    def _setup_ui(self):
        """Builds a clean, modern, and resizable layout."""
        # Main container frame
        main_frame = ttk.Frame(self, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Header Label
        header = ttk.Label(
            main_frame, 
            text="Document Ingestion Error Ledger", 
            font=("Helvetica", 14, "bold")
        )
        header.pack(anchor=tk.W, pady=(0, 10))

        # Table & Scrollbar Container
        table_frame = ttk.Frame(main_frame)
        table_frame.pack(fill=tk.BOTH, expand=True)

        # Scrollbars
        v_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL)
        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        h_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL)
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X)

        # Treeview (Table) setup
        columns = ("timestamp", "file_name", "error_type", "details", "source_url")
        self.tree = ttk.Treeview(
            table_frame, 
            columns=columns, 
            show="headings", 
            selectmode="browse",
            yscrollcommand=v_scroll.set,
            xscrollcommand=h_scroll.set
        )
        
        v_scroll.config(command=self.tree.yview)
        h_scroll.config(command=self.tree.xview)

        # Define headers and widths
        self.tree.heading("timestamp", text="Timestamp")
        self.tree.heading("file_name", text="File Name")
        self.tree.heading("error_type", text="Error Type")
        self.tree.heading("details", text="Details")
        self.tree.heading("source_url", text="Source URL")

        self.tree.column("timestamp", width=130, minwidth=100, stretch=False)
        self.tree.column("file_name", width=180, minwidth=120)
        self.tree.column("error_type", width=120, minwidth=100, stretch=False)
        self.tree.column("details", width=300, minwidth=150)
        self.tree.column("source_url", width=150, minwidth=100)

        self.tree.pack(fill=tk.BOTH, expand=True)

        # Double-click binding to open the URL directly
        self.tree.bind("<Double-1>", self.open_selected_url)

        # Bottom Control Panel
        btn_frame = ttk.Frame(main_frame, padding="5")
        btn_frame.pack(fill=tk.X, pady=(10, 0))

        # Info Label (Help tip)
        info_lbl = ttk.Label(
            btn_frame, 
            text="💡 Tip: Double-click a row to visit the document source URL", 
            font=("Helvetica", 9, "italic")
        )
        info_lbl.pack(side=tk.LEFT)

        # Control Buttons
        close_btn = ttk.Button(btn_frame, text="Close", command=self.destroy)
        close_btn.pack(side=tk.RIGHT, padx=5)

        clear_btn = ttk.Button(btn_frame, text="Clear Ledger", command=self.clear_ledger)
        clear_btn.pack(side=tk.RIGHT, padx=5)

        refresh_btn = ttk.Button(btn_frame, text="Refresh", command=self.load_errors)
        refresh_btn.pack(side=tk.RIGHT, padx=5)

    def load_errors(self):
        """Loads and parses the JSON log file safely without crashing on lock or format corruption."""
        # Clear current list
        for item in self.tree.get_children():
            self.tree.delete(item)

        if not self.ledger_path.exists():
            # Nothing to load yet
            return

        try:
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                errors = json.load(f)
                
            # Populating from newest to oldest
            for err in reversed(errors):
                self.tree.insert(
                    "", 
                    tk.END, 
                    values=(
                        err.get("timestamp", "N/A"),
                        err.get("file_name", "N/A"),
                        err.get("error_type", "N/A"),
                        err.get("details", "N/A"),
                        err.get("source_url", "None")
                    )
                )
        except json.JSONDecodeError:
            messagebox.showerror("Error", "Error ledger JSON file is corrupted.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load ledger: {e}")

    def open_selected_url(self, event=None):
        """Handles double-click to visit the URL stored in the row."""
        selected_item = self.tree.focus()
        if not selected_item:
            return

        values = self.tree.item(selected_item, "values")
        url = values[4] if len(values) > 4 else "None"

        if url and url != "None" and url.startswith(("http://", "https://")):
            webbrowser.open(url)
        else:
            messagebox.showinfo("No Link", "This error does not have a valid web source URL.")

    def clear_ledger(self):
        """Prompts user confirmation and deletes the log file."""
        if not self.ledger_path.exists():
            return

        confirm = messagebox.askyesno(
            "Clear Ledger", 
            "Are you sure you want to permanently delete the ingestion error ledger?",
            parent=self
        )
        if confirm:
            try:
                self.ledger_path.unlink(missing_ok=True)
                self.load_errors()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to delete file: {e}")