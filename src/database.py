import sqlite3
import json
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    source_path TEXT,
    source_url TEXT,
    virtual_folder TEXT,
    file_type TEXT,
    entities_json TEXT,
    citations_json TEXT,
    keywords_json TEXT,
    text TEXT,
    cluster_id INTEGER,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP,
    deleted_original INTEGER DEFAULT 0,
    held_no_repull_source INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS priority_queue (
    citation TEXT PRIMARY KEY,
    source_doc_id TEXT,
    priority_score INTEGER DEFAULT 1,
    status TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS citation_index (
    citation TEXT PRIMARY KEY,
    doc_id TEXT
);

CREATE TABLE IF NOT EXISTS cross_references (
    from_doc_id TEXT,
    to_doc_id TEXT,
    citation TEXT,
    PRIMARY KEY (from_doc_id, to_doc_id, citation)
);

CREATE TABLE IF NOT EXISTS ref_counter (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_value INTEGER DEFAULT 1
);
"""
class DB:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        try:
            self.conn.execute("ALTER TABLE documents ADD COLUMN ref_no TEXT")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        self._backfill_ref_numbers()

    def add_to_priority_queue(self, citation, status="SYSTEM_SEED"):
        self.conn.execute(
            "INSERT OR IGNORE INTO priority_queue (citation, source_doc_id) VALUES (?, ?)",
            (citation, status)
        )
        self.conn.commit()

    def get_next_priority_citation(self):
     cursor = self.conn.cursor()
     # This filter is the missing link to stop the loop
     cursor.execute("SELECT citation FROM priority_queue WHERE status IS NULL OR status = 'pending' LIMIT 1")
     row = cursor.fetchone()
     return row[0] if row else None

    def mark_priority_fetched(self, citation):
        self.conn.execute("UPDATE priority_queue SET status = 'fetched' WHERE citation = ?", (citation,))
        self.conn.commit()

    def check_citation_indexed(self, citation):
        cursor = self.conn.execute("SELECT 1 FROM priority_queue WHERE citation = ?", (citation,))
        return cursor.fetchone() is not None

    def get_document(self, doc_id):
        """Checks if a document ID already exists in the database."""
        cursor = self.conn.execute("SELECT id FROM documents WHERE id = ?", (doc_id,))
        return cursor.fetchone()

    def all_texts_except(self, doc_id):
        """Retrieves all document texts from the database except the active one."""
        cursor = self.conn.execute("SELECT text FROM documents WHERE id != ?", (doc_id,))
        rows = cursor.fetchall()
        return [row[0] for row in rows]

    def insert_document(self, doc_id, source_path, file_type, entities, citations, keywords, text, source_url=None, virtual_folder=None):
        """Glues together and saves the fully parsed document metadata into the database."""
        self.conn.execute(
            """INSERT OR REPLACE INTO documents 
            (id, source_path, file_type, entities_json, citations_json, keywords_json, text, source_url, virtual_folder) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc_id, source_path, file_type, 
                json.dumps(entities), json.dumps(citations), json.dumps(keywords), 
                text, source_url, virtual_folder
            )
        )
        self.conn.commit()

    def mark_deleted_original(self, doc_id):
        """Flags that the raw local file was cleared because a web backup source is known."""
        self.conn.execute("UPDATE documents SET deleted_original = 1 WHERE id = ?", (doc_id,))
        self.conn.commit()

    def mark_held_no_source(self, doc_id):
        """Flags that the file is safely held locally because no online backup exists."""
        self.conn.execute("UPDATE documents SET held_no_repull_source = 1 WHERE id = ?", (doc_id,))
        self.conn.commit()

    def mark_priority_failed(self, citation: str):
    	"""Updates the database so this citation is not attempted again."""
    	cursor = self.conn.cursor()
    	cursor.execute("UPDATE priority_queue SET status = 'failed' WHERE citation = ?", (citation,))
    	self.conn.commit()

# ---------- reference numbers ----------
    def get_next_ref_no(self) -> str:
        self.conn.execute("INSERT OR IGNORE INTO ref_counter (id, next_value) VALUES (1, 1)")
        row = self.conn.execute("SELECT next_value FROM ref_counter WHERE id=1").fetchone()
        current = row[0]
        self.conn.execute("UPDATE ref_counter SET next_value = ? WHERE id=1", (current + 1,))
        self.conn.commit()
        return f"LC-{current:06d}"

    def _backfill_ref_numbers(self):
        """One-time catch-up: assigns ref numbers to documents indexed
        before this feature existed."""
        rows = self.conn.execute(
            "SELECT id FROM documents WHERE ref_no IS NULL ORDER BY added_at"
        ).fetchall()
        for (doc_id,) in rows:
            ref_no = self.get_next_ref_no()
            self.conn.execute("UPDATE documents SET ref_no=? WHERE id=?", (ref_no, doc_id))
        if rows:
            self.conn.commit()

    def assign_ref_no(self, doc_id: str) -> str:
        ref_no = self.get_next_ref_no()
        self.conn.execute("UPDATE documents SET ref_no=? WHERE id=?", (ref_no, doc_id))
        self.conn.commit()
        return ref_no

    def get_ref_no(self, doc_id: str) -> str | None:
        row = self.conn.execute("SELECT ref_no FROM documents WHERE id=?", (doc_id,)).fetchone()
        return row[0] if row else None

    # ---------- cross-referencing ----------
    def register_self_citation(self, citation: str, doc_id: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO citation_index (citation, doc_id) VALUES (?, ?)",
            (citation, doc_id),
        )
        self.conn.commit()

    def lookup_citation(self, citation: str) -> str | None:
        row = self.conn.execute(
            "SELECT doc_id FROM citation_index WHERE citation=?", (citation,)
        ).fetchone()
        return row[0] if row else None

    def add_cross_reference(self, from_doc_id: str, to_doc_id: str, citation: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO cross_references (from_doc_id, to_doc_id, citation) VALUES (?, ?, ?)",
            (from_doc_id, to_doc_id, citation),
        )
        self.conn.commit()

    def get_cross_references(self, doc_id: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT cr.to_doc_id, d.ref_no, cr.citation FROM cross_references cr
               JOIN documents d ON d.id = cr.to_doc_id
               WHERE cr.from_doc_id=?""",
            (doc_id,),
        ).fetchall()
        return [{"doc_id": r[0], "ref_no": r[1], "citation": r[2]} for r in rows]