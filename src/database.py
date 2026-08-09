import json
import sqlite3
import time
from pathlib import Path

from .citation_history import normalize_citation

LEGAL_SORTER_DB_PATH = r"C:\LegalSorter\index\legal_sorter.db"
SQLITE_RETRY_ATTEMPTS = 5
SQLITE_RETRY_SLEEP_SECONDS = 0.15


def resolve_db_path(db_path: str | None = None) -> str:
    return db_path or LEGAL_SORTER_DB_PATH


def connect_sqlite(db_path: str | None = None) -> sqlite3.Connection:
    resolved = resolve_db_path(db_path)
    Path(resolved).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(resolved, timeout=30, check_same_thread=False)


def safe_connection_execute(conn: sqlite3.Connection, sql, params=()):
    for attempt in range(SQLITE_RETRY_ATTEMPTS):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt == SQLITE_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(SQLITE_RETRY_SLEEP_SECONDS)


def safe_connection_commit(conn: sqlite3.Connection):
    for attempt in range(SQLITE_RETRY_ATTEMPTS):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt == SQLITE_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(SQLITE_RETRY_SLEEP_SECONDS)

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
    held_no_repull_source INTEGER DEFAULT 0,
    content_source TEXT DEFAULT 'full_text',
    sanity_check_passed INTEGER DEFAULT 1
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

CREATE TABLE IF NOT EXISTS citation_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_doc_id TEXT NOT NULL,
    to_doc_id TEXT,
    citation TEXT NOT NULL,
    citation_key TEXT,
    treatment TEXT DEFAULT 'cited',
    context_snippet TEXT DEFAULT '',
    UNIQUE (from_doc_id, citation_key, citation)
);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id TEXT PRIMARY KEY,
    source_path TEXT,
    source_url TEXT,
    state TEXT,
    doc_id TEXT,
    content_hash TEXT,
    source_fingerprint TEXT,
    source_mtime TEXT,
    attempts INTEGER DEFAULT 0,
    quarantined_path TEXT,
    error_type TEXT,
    error_details TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS backup_history (
    backup_id TEXT PRIMARY KEY,
    archive_path TEXT,
    verified_ok INTEGER DEFAULT 0,
    details_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ref_counter (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_value INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_documents_added_at ON documents (added_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_virtual_folder ON documents (virtual_folder);
CREATE INDEX IF NOT EXISTS idx_priority_queue_status ON priority_queue (status);
CREATE INDEX IF NOT EXISTS idx_documents_source_url ON documents (source_url);
CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_state ON ingestion_jobs (state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_doc_id ON ingestion_jobs (doc_id);
CREATE INDEX IF NOT EXISTS idx_citation_relationships_from_doc ON citation_relationships (from_doc_id);
CREATE INDEX IF NOT EXISTS idx_citation_relationships_to_doc ON citation_relationships (to_doc_id);
CREATE INDEX IF NOT EXISTS idx_citation_relationships_key ON citation_relationships (citation_key);
CREATE INDEX IF NOT EXISTS idx_citation_relationships_treatment ON citation_relationships (treatment);
"""

FTS_SETUP = """
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    id UNINDEXED,
    ref_no UNINDEXED,
    virtual_folder,
    keywords_json,
    text,
    content='documents',
    content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, id, ref_no, virtual_folder, keywords_json, text)
    VALUES (new.rowid, new.id, new.ref_no, new.virtual_folder, new.keywords_json, new.text);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, id, ref_no, virtual_folder, keywords_json, text)
    VALUES ('delete', old.rowid, old.id, old.ref_no, old.virtual_folder, old.keywords_json, old.text);
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, id, ref_no, virtual_folder, keywords_json, text)
    VALUES ('delete', old.rowid, old.id, old.ref_no, old.virtual_folder, old.keywords_json, old.text);
    INSERT INTO documents_fts(rowid, id, ref_no, virtual_folder, keywords_json, text)
    VALUES (new.rowid, new.id, new.ref_no, new.virtual_folder, new.keywords_json, new.text);
END;
"""
class DB:
    def __init__(self, db_path: str):
        db_path = resolve_db_path(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # timeout=30: wait up to 30 s for another writer to release the lock
        # before raising OperationalError, preventing crashes when run.py and
        # bulk_ingest.py both write to the same DB concurrently.
        self.conn = connect_sqlite(db_path)
        # WAL mode allows readers and writers to coexist without blocking each other.
        # busy_timeout mirrors the Python-level timeout inside SQLite itself.
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.safe_execute("PRAGMA busy_timeout=30000;")
        self.safe_execute("PRAGMA cache_size=-32000")  # ~32 MB page cache
        self.conn.executescript(SCHEMA)
        self.safe_commit()

        # FTS5 virtual table for fast full-text search (created separately to avoid
        # executescript conflicts with triggers that reference other tables)
        try:
            self.conn.executescript(FTS_SETUP)
            self.safe_commit()
        except Exception:
            pass  # FTS already exists or SQLite build lacks FTS5 — degrade gracefully
        try:
            self.safe_execute("ALTER TABLE documents ADD COLUMN ref_no TEXT")
            self.safe_commit()
        except sqlite3.OperationalError:
            pass  # column already exists

        # --- SAFE BOOKMARK & USER FOLDER MIGRATION SCRIPT ---
        try:
            self.safe_execute("ALTER TABLE documents ADD COLUMN is_bookmarked INTEGER DEFAULT 0")
            self.safe_execute("ALTER TABLE documents ADD COLUMN user_folder TEXT")
            self.safe_commit()
        except sqlite3.OperationalError:
            pass  # columns already exist

        # --- CONTENT QUALITY COLUMNS (added with content-sanity-check feature) ---
        for col_def in [
            "content_source TEXT DEFAULT 'full_text'",
            "sanity_check_passed INTEGER DEFAULT 1",
            "source_fingerprint TEXT",
            "document_version INTEGER DEFAULT 1",
        ]:
            col_name = col_def.split()[0]
            try:
                self.safe_execute(f"ALTER TABLE documents ADD COLUMN {col_def}")
                self.safe_commit()
            except sqlite3.OperationalError:
                pass  # column already exists

        # Ensure ref_no has an index for fast UI lookups
        try:
            self.safe_execute("CREATE INDEX IF NOT EXISTS idx_documents_ref_no ON documents (ref_no)")
            self.safe_commit()
        except Exception:
            pass

        self._backfill_ref_numbers()

        # --- STRUCTURED BARCODE COLUMNS ---
        # barcode:            the LS-CT-JR-SM-YR-SQ structured ID
        # barcode_strategy:   'rules' or 'llm' — which generator produced it
        # barcode_confirmed:  0 = auto-generated, 1 = manually reviewed/corrected
        # barcode_confidence: float 0.0–1.0 classification confidence
        for col_def in [
            "barcode TEXT",
            "barcode_strategy TEXT DEFAULT 'rules'",
            "barcode_confirmed INTEGER DEFAULT 0",
            "barcode_confidence REAL DEFAULT 0.0",
        ]:
            try:
                self.safe_execute(f"ALTER TABLE documents ADD COLUMN {col_def}")
                self.safe_commit()
            except sqlite3.OperationalError:
                pass  # column already exists

        # B-tree index on barcode for fast prefix scans (WHERE barcode LIKE 'LS-CA-%')
        try:
            self.safe_execute(
                "CREATE INDEX IF NOT EXISTS idx_documents_barcode ON documents (barcode)"
            )
            self.safe_commit()
        except Exception:
            pass

        for col_def in [
            "citation_key TEXT",
            "treatment TEXT DEFAULT 'cited'",
            "context_snippet TEXT DEFAULT ''",
        ]:
            try:
                self.safe_execute(f"ALTER TABLE citation_relationships ADD COLUMN {col_def}")
                self.safe_commit()
            except sqlite3.OperationalError:
                pass

        try:
            self.safe_execute("ALTER TABLE citation_index ADD COLUMN citation_key TEXT")
            self.safe_commit()
        except sqlite3.OperationalError:
            pass
        try:
            self.safe_execute(
                "CREATE INDEX IF NOT EXISTS idx_citation_index_key ON citation_index (citation_key)"
            )
            self.safe_commit()
        except Exception:
            pass

        self._backfill_citation_index_keys()

        self._backfill_barcodes()

    def safe_execute(self, sql, params=()):
        return safe_connection_execute(self.conn, sql, params)

    def safe_commit(self):
        safe_connection_commit(self.conn)

    def add_to_priority_queue(self, citation, status="SYSTEM_SEED"):
        self.safe_execute(
            "INSERT OR IGNORE INTO priority_queue (citation, source_doc_id) VALUES (?, ?)",
            (citation, status)
        )
        self.safe_commit()

    def get_next_priority_citation(self):
        # This filter is the missing link to stop the loop
        cursor = self.safe_execute("SELECT citation FROM priority_queue WHERE status IS NULL OR status = 'pending' LIMIT 1")
        row = cursor.fetchone()
        return row[0] if row else None

    def mark_priority_fetched(self, citation):
        self.safe_execute("UPDATE priority_queue SET status = 'fetched' WHERE citation = ?", (citation,))
        self.safe_commit()

    def check_citation_indexed(self, citation):
        cursor = self.safe_execute("SELECT 1 FROM priority_queue WHERE citation = ?", (citation,))
        return cursor.fetchone() is not None

    def get_document(self, doc_id):
        """Checks if a document ID already exists in the database."""
        cursor = self.safe_execute("SELECT id FROM documents WHERE id = ?", (doc_id,))
        return cursor.fetchone()

    def all_texts_except(self, doc_id):
        """Retrieves up to 200 document text samples (first 8 000 chars each) from the
        database except the active one. Capping both count and length keeps TF-IDF fast
        regardless of archive size while still giving the vectoriser a representative corpus."""
        cursor = self.safe_execute(
            "SELECT SUBSTR(text, 1, 8000) FROM documents WHERE id != ? AND text IS NOT NULL LIMIT 200",
            (doc_id,)
        )
        rows = cursor.fetchall()
        return [row[0] for row in rows]

    def insert_document(self, doc_id, source_path, file_type, entities, citations, keywords, text,
                        source_url=None, virtual_folder=None,
                        content_source="full_text", sanity_check_passed=1,
                        source_fingerprint=None, document_version=1):
        """Glues together and saves the fully parsed document metadata into the database."""
        self.safe_execute(
            """INSERT OR REPLACE INTO documents 
            (id, source_path, file_type, entities_json, citations_json, keywords_json, text,
             source_url, virtual_folder, content_source, sanity_check_passed,
             source_fingerprint, document_version) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc_id, source_path, file_type,
                json.dumps(entities), json.dumps(citations), json.dumps(keywords),
                text, source_url, virtual_folder, content_source, sanity_check_passed,
                source_fingerprint, document_version,
            )
        )
        self.safe_commit()

    def mark_deleted_original(self, doc_id):
        """Flags that the raw local file was cleared because a web backup source is known."""
        self.safe_execute("UPDATE documents SET deleted_original = 1 WHERE id = ?", (doc_id,))
        self.safe_commit()

    def mark_held_no_source(self, doc_id):
        """Flags that the file is safely held locally because no online backup exists."""
        self.safe_execute("UPDATE documents SET held_no_repull_source = 1 WHERE id = ?", (doc_id,))
        self.safe_commit()

    def mark_priority_failed(self, citation: str):
        """Updates the database so this citation is not attempted again."""
        self.safe_execute("UPDATE priority_queue SET status = 'failed' WHERE citation = ?", (citation,))
        self.safe_commit()

# ---------- reference numbers ----------
    def get_next_ref_no(self) -> str:
        self.safe_execute("INSERT OR IGNORE INTO ref_counter (id, next_value) VALUES (1, 1)")
        row = self.safe_execute("SELECT next_value FROM ref_counter WHERE id=1").fetchone()
        current = row[0]
        self.safe_execute("UPDATE ref_counter SET next_value = ? WHERE id=1", (current + 1,))
        self.safe_commit()
        return f"LC-{current:06d}"

    def _backfill_ref_numbers(self):
        """One-time catch-up: assigns ref numbers to documents indexed
        before this feature existed."""
        rows = self.safe_execute(
            "SELECT id FROM documents WHERE ref_no IS NULL ORDER BY added_at"
        ).fetchall()
        for (doc_id,) in rows:
            ref_no = self.get_next_ref_no()
            self.safe_execute("UPDATE documents SET ref_no=? WHERE id=?", (ref_no, doc_id))
        if rows:
            self.safe_commit()

    def assign_ref_no(self, doc_id: str) -> str:
        ref_no = self.get_next_ref_no()
        self.safe_execute("UPDATE documents SET ref_no=? WHERE id=?", (ref_no, doc_id))
        self.safe_commit()
        return ref_no

    def get_ref_no(self, doc_id: str) -> str | None:
        row = self.safe_execute("SELECT ref_no FROM documents WHERE id=?", (doc_id,)).fetchone()
        return row[0] if row else None

# ---------- structured barcode ----------
    def set_barcode(
        self,
        doc_id: str,
        barcode: str,
        strategy: str = "rules",
        confidence: float = 0.0,
        confirm_threshold: float = 0.85,
    ) -> str:
        """Persist the structured barcode ID for a document.

        Collision handling: if *barcode* is already assigned to a *different*
        document, a single-letter suffix (A–Z) is appended to the SQ segment
        until a free slot is found.

        Auto-confirmation: sets barcode_confirmed = 1 when confidence is at or
        above *confirm_threshold* (default 0.85).

        Returns the final barcode string that was stored (may differ from the
        input when a collision suffix was appended).
        """
        try:
            from . import barcode as barcode_mod
        except ImportError:
            import barcode as barcode_mod  # type: ignore[no-redef]

        # Determine the base barcode (strip any existing single-letter suffix)
        # then query only the collision candidates (exact + A–Z) for this base.
        base_bc = barcode[:-1] if (len(barcode) > 1 and barcode[-1].isupper()) else barcode
        like_pattern = base_bc.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.safe_execute(
            "SELECT barcode FROM documents WHERE barcode LIKE ? ESCAPE '\\' AND id != ?",
            (like_pattern + "%", doc_id),
        ).fetchall()
        existing: set[str] = {r[0] for r in rows if r[0]}
        barcode = barcode_mod.resolve_collision(barcode, existing)

        confirmed = 1 if confidence >= confirm_threshold else 0
        self.safe_execute(
            """UPDATE documents
               SET barcode=?, barcode_strategy=?, barcode_confidence=?,
                   barcode_confirmed=?
               WHERE id=?""",
            (barcode, strategy, confidence, confirmed, doc_id),
        )
        self.safe_commit()
        return barcode

    def get_barcode(self, doc_id: str) -> str | None:
        """Return the barcode for a document, or None if not yet assigned."""
        row = self.safe_execute(
            "SELECT barcode FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        return row[0] if row else None

    def confirm_barcode(self, doc_id: str, barcode: str) -> None:
        """Manually confirm (and optionally correct) a barcode.

        Sets barcode_confirmed=1 and barcode_confidence=1.0 so the record is
        excluded from future automatic backfill or regeneration passes.
        """
        self.safe_execute(
            "UPDATE documents SET barcode=?, barcode_confirmed=1, barcode_confidence=1.0 WHERE id=?",
            (barcode, doc_id),
        )
        self.safe_commit()

    def _backfill_barcodes(self) -> None:
        """One-time rule-based catch-up: assigns barcodes to documents that
        were indexed before this feature existed.  Confirmed barcodes are
        never overwritten.  Uses only the rule engine (no LLM) to keep
        startup fast regardless of network availability.
        """
        try:
            from . import barcode as barcode_mod  # relative import (package context)
        except ImportError:
            try:
                import barcode as barcode_mod  # direct-run / test context
            except ImportError:
                return  # barcode module not available yet — skip silently

        rows = self.safe_execute(
            """SELECT id, ref_no, entities_json, keywords_json, virtual_folder, text
               FROM documents
               WHERE barcode IS NULL AND (barcode_confirmed IS NULL OR barcode_confirmed = 0)
               ORDER BY added_at""",
        ).fetchall()

        for (doc_id, ref_no, entities_raw, keywords_raw, virtual_folder, text) in rows:
            try:
                entities = json.loads(entities_raw) if entities_raw else {}
                keywords = json.loads(keywords_raw) if keywords_raw else []
                barcode, strategy, confidence = barcode_mod.assign_barcode(
                    text=text or "",
                    entities=entities,
                    citations=[],
                    keywords=keywords,
                    ref_no=ref_no or "",
                    cfg={},  # rule-engine only during backfill (no cfg → no LLM)
                    virtual_folder=virtual_folder or "",
                )
                # Use set_barcode so collision handling and auto-confirm run
                self.set_barcode(doc_id, barcode, strategy=strategy, confidence=confidence)
            except Exception:
                pass  # never let a backfill error block startup

    def barcode_prefix_search(
        self,
        prefix: str,
        limit: int = 500,
    ) -> list[dict]:
        """Return documents whose barcode starts with *prefix*.

        Uses the B-tree index on the barcode column, so this is O(log n)
        regardless of archive size.

        Returns a list of dicts with keys:
            id, ref_no, barcode, barcode_strategy, barcode_confidence,
            virtual_folder, source_url
        """
        # Escape SQL LIKE special characters in the prefix
        like_safe = (
            prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        rows = self.safe_execute(
            """SELECT id, ref_no, barcode, barcode_strategy, barcode_confidence,
                      virtual_folder, source_url
               FROM documents
               WHERE barcode LIKE ? ESCAPE '\\'
               ORDER BY barcode
               LIMIT ?""",
            (like_safe + "%", limit),
        ).fetchall()
        return [
            {
                "id": r[0],
                "ref_no": r[1],
                "barcode": r[2],
                "barcode_strategy": r[3],
                "barcode_confidence": r[4],
                "virtual_folder": r[5],
                "source_url": r[6],
            }
            for r in rows
        ]

    def get_document_by_barcode(self, barcode: str) -> dict | None:
        """Fetch a single document by its exact barcode.

        Returns a dict with the full document row, or None if not found.
        """
        row = self.safe_execute(
            """SELECT id, ref_no, barcode, barcode_strategy, barcode_confirmed,
                      barcode_confidence, virtual_folder, source_url, file_type,
                      keywords_json, citations_json, entities_json, added_at
               FROM documents WHERE barcode = ?""",
            (barcode,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "ref_no": row[1],
            "barcode": row[2],
            "barcode_strategy": row[3],
            "barcode_confirmed": bool(row[4]),
            "barcode_confidence": row[5],
            "virtual_folder": row[6],
            "source_url": row[7],
            "file_type": row[8],
            "keywords": json.loads(row[9]) if row[9] else [],
            "citations": json.loads(row[10]) if row[10] else [],
            "entities": json.loads(row[11]) if row[11] else {},
            "added_at": row[12],
        }

    def find_docs_needing_barcode_regen(
        self,
        min_confidence: float = 0.85,
        force: bool = False,
    ) -> list[dict]:
        """Return documents that need barcode re-generation.

        A document is selected when any of these conditions hold:
          - barcode IS NULL (never generated)
          - barcode_strategy = 'failed' (previous attempt errored)
          - barcode_confidence < min_confidence (low-quality barcode)

        When *force* is True, confirmed barcodes are also included.

        Returns a list of dicts with keys:
            id, ref_no, barcode, barcode_confidence, barcode_confirmed,
            entities_json, keywords_json, virtual_folder, text
        """
        base = """
            SELECT id, ref_no, barcode, barcode_confidence, barcode_confirmed,
                   entities_json, keywords_json, virtual_folder, text
            FROM documents
            WHERE (
                barcode IS NULL
                OR barcode_strategy = 'failed'
                OR (barcode_confidence IS NOT NULL AND barcode_confidence < ?)
            )
        """
        params: list = [min_confidence]
        if not force:
            base += " AND (barcode_confirmed IS NULL OR barcode_confirmed = 0)"
        base += " ORDER BY added_at"
        rows = self.safe_execute(base, params).fetchall()
        return [
            {
                "id": r[0],
                "ref_no": r[1],
                "barcode": r[2],
                "barcode_confidence": r[3],
                "barcode_confirmed": bool(r[4]),
                "entities_json": r[5],
                "keywords_json": r[6],
                "virtual_folder": r[7],
                "text": r[8],
            }
            for r in rows
        ]

    # ---------- cross-referencing ----------
    def register_self_citation(self, citation: str, doc_id: str):
        citation_key = normalize_citation(citation)
        self.safe_execute(
            """INSERT OR IGNORE INTO citation_index (citation, doc_id, citation_key)
               VALUES (?, ?, ?)""",
            (citation, doc_id, citation_key),
        )
        self.safe_commit()
        if citation_key:
            self.resolve_pending_citation_relationships(doc_id, citation, citation_key)

    def lookup_citation(self, citation: str) -> str | None:
        row = self.safe_execute(
            "SELECT doc_id FROM citation_index WHERE citation=?", (citation,)
        ).fetchone()
        if row:
            return row[0]
        citation_key = normalize_citation(citation)
        if not citation_key:
            return None
        row = self.safe_execute(
            "SELECT doc_id FROM citation_index WHERE citation_key=? ORDER BY rowid DESC LIMIT 1",
            (citation_key,),
        ).fetchone()
        return row[0] if row else None

    def add_cross_reference(
        self,
        from_doc_id: str,
        to_doc_id: str | None,
        citation: str,
        *,
        treatment: str = "cited",
        context_snippet: str = "",
        citation_key: str | None = None,
    ):
        citation_key = citation_key or normalize_citation(citation)
        self.safe_execute(
            """INSERT INTO citation_relationships
               (from_doc_id, to_doc_id, citation, citation_key, treatment, context_snippet)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(from_doc_id, citation_key, citation) DO UPDATE SET
                 to_doc_id=excluded.to_doc_id,
                 treatment=excluded.treatment,
                 context_snippet=excluded.context_snippet""",
            (from_doc_id, to_doc_id, citation, citation_key, treatment, context_snippet[:280]),
        )
        if to_doc_id:
            self.safe_execute(
                "INSERT OR IGNORE INTO cross_references (from_doc_id, to_doc_id, citation) VALUES (?, ?, ?)",
                (from_doc_id, to_doc_id, citation),
            )
            self.safe_execute(
                "DELETE FROM cross_references WHERE from_doc_id=? AND citation=? AND (to_doc_id IS NULL OR to_doc_id!=?)",
                (from_doc_id, citation, to_doc_id),
            )
        self.safe_commit()

    def get_cross_references(self, doc_id: str) -> list[dict]:
        rows = self.safe_execute(
            """SELECT cr.to_doc_id, d.ref_no, cr.citation, d.barcode, d.entities_json
               FROM citation_relationships cr
               JOIN documents d ON d.id = cr.to_doc_id
               WHERE cr.from_doc_id=? AND cr.to_doc_id IS NOT NULL
               ORDER BY d.added_at DESC""",
            (doc_id,),
        ).fetchall()
        return [
            {
                "doc_id": r[0],
                "ref_no": r[1],
                "citation": r[2],
                "barcode": r[3],
                "year": self._derive_case_year(r[3], r[4]),
            }
            for r in rows
        ]

    def get_subsequent_history(self, doc_id: str, limit: int = 20) -> list[dict]:
        rows = self.safe_execute(
            """SELECT cr.from_doc_id, d.ref_no, d.barcode, d.entities_json,
                      cr.citation, cr.treatment, cr.context_snippet
               FROM citation_relationships cr
               JOIN documents d ON d.id = cr.from_doc_id
               WHERE cr.to_doc_id = ?
               ORDER BY
                 CASE cr.treatment
                   WHEN 'overruled' THEN 0
                   WHEN 'criticized' THEN 1
                   WHEN 'limited' THEN 2
                   WHEN 'distinguished' THEN 3
                   WHEN 'followed' THEN 4
                   WHEN 'cited' THEN 5
                   ELSE 6
                 END,
                 d.added_at DESC
               LIMIT ?""",
            (doc_id, limit),
        ).fetchall()
        return [
            {
                "doc_id": r[0],
                "ref_no": r[1],
                "barcode": r[2],
                "year": self._derive_case_year(r[2], r[3]),
                "citation": r[4],
                "treatment": r[5] or "cited",
                "context": r[6] or "",
            }
            for r in rows
        ]

    def get_subsequent_history_summary(self, doc_id: str, limit: int = 3) -> dict:
        items = self.get_subsequent_history(doc_id, limit=limit)
        negative_count = self.safe_execute(
            """SELECT COUNT(*) FROM citation_relationships
               WHERE to_doc_id = ? AND treatment IN ('overruled', 'criticized', 'limited')""",
            (doc_id,),
        ).fetchone()[0]
        total_count = self.safe_execute(
            "SELECT COUNT(*) FROM citation_relationships WHERE to_doc_id = ?",
            (doc_id,),
        ).fetchone()[0]
        return {
            "count": int(total_count or 0),
            "negative_treatment_count": int(negative_count or 0),
            "items": items,
        }

    def get_subsequent_history_summary_map(self, doc_ids: list[str], limit: int = 3) -> dict[str, dict]:
        cleaned = [doc_id for doc_id in doc_ids if doc_id]
        if not cleaned:
            return {}
        placeholders = ",".join("?" for _ in cleaned)
        rows = self.safe_execute(
            f"""SELECT cr.to_doc_id, cr.from_doc_id, d.ref_no, d.barcode, d.entities_json,
                       cr.citation, cr.treatment, cr.context_snippet
                FROM citation_relationships cr
                JOIN documents d ON d.id = cr.from_doc_id
                WHERE cr.to_doc_id IN ({placeholders})
                ORDER BY cr.to_doc_id,
                  CASE cr.treatment
                    WHEN 'overruled' THEN 0
                    WHEN 'criticized' THEN 1
                    WHEN 'limited' THEN 2
                    WHEN 'distinguished' THEN 3
                    WHEN 'followed' THEN 4
                    WHEN 'cited' THEN 5
                    ELSE 6
                  END,
                  d.added_at DESC""",
            cleaned,
        ).fetchall()
        summary = {
            doc_id: {"count": 0, "negative_treatment_count": 0, "items": []}
            for doc_id in cleaned
        }
        for row in rows:
            target_doc_id = row[0]
            bucket = summary.setdefault(
                target_doc_id, {"count": 0, "negative_treatment_count": 0, "items": []}
            )
            bucket["count"] += 1
            treatment = row[6] or "cited"
            if treatment in {"overruled", "criticized", "limited"}:
                bucket["negative_treatment_count"] += 1
            if len(bucket["items"]) < limit:
                bucket["items"].append(
                    {
                        "doc_id": row[1],
                        "ref_no": row[2],
                        "barcode": row[3],
                        "year": self._derive_case_year(row[3], row[4]),
                        "citation": row[5],
                        "treatment": treatment,
                        "context": row[7] or "",
                    }
                )
        return summary

    def clear_citation_relationships_for_doc(self, doc_id: str) -> None:
        self.safe_execute("DELETE FROM citation_relationships WHERE from_doc_id = ?", (doc_id,))
        self.safe_execute("DELETE FROM cross_references WHERE from_doc_id = ?", (doc_id,))
        self.safe_commit()

    def resolve_pending_citation_relationships(
        self,
        target_doc_id: str,
        citation: str,
        citation_key: str | None = None,
    ) -> int:
        citation_key = citation_key or normalize_citation(citation)
        if not citation_key:
            return 0
        cur = self.safe_execute(
            """UPDATE citation_relationships
               SET to_doc_id = ?
               WHERE citation_key = ? AND (to_doc_id IS NULL OR to_doc_id = '') AND from_doc_id != ?""",
            (target_doc_id, citation_key, target_doc_id),
        )
        self.safe_execute(
            """INSERT OR IGNORE INTO cross_references (from_doc_id, to_doc_id, citation)
               SELECT from_doc_id, ?, citation
               FROM citation_relationships
               WHERE citation_key = ? AND to_doc_id = ? AND from_doc_id != ?""",
            (target_doc_id, citation_key, target_doc_id, target_doc_id),
        )
        self.safe_commit()
        return int(cur.rowcount or 0)

    def rebuild_citation_relationships(self) -> dict:
        from . import tagger
        from .citation_history import extract_citation_relationships

        rows = self.safe_execute(
            "SELECT id, text, citations_json FROM documents ORDER BY added_at"
        ).fetchall()
        self.safe_execute("DELETE FROM citation_relationships")
        self.safe_execute("DELETE FROM cross_references")
        self.safe_execute("DELETE FROM citation_index")
        self.safe_commit()

        indexed = 0
        relationships = 0
        unresolved = 0
        prepared: list[tuple[str, str, list[str], str | None]] = []
        for doc_id, text, citations_raw in rows:
            try:
                citations = json.loads(citations_raw) if citations_raw else []
            except Exception:
                citations = []
            self_citation = tagger.extract_self_citation(text or "", citations)
            if self_citation:
                self.register_self_citation(self_citation, doc_id)
                indexed += 1
            prepared.append((doc_id, text or "", citations, self_citation))

        for doc_id, text, citations, self_citation in prepared:
            for rel in extract_citation_relationships(text, citations, self_citation=self_citation):
                to_doc_id = self.lookup_citation(rel["citation"])
                self.add_cross_reference(
                    doc_id,
                    to_doc_id,
                    rel["citation"],
                    treatment=rel["treatment"],
                    context_snippet=rel["context"],
                    citation_key=rel["citation_key"],
                )
                relationships += 1
                if not to_doc_id:
                    unresolved += 1
        return {
            "documents": len(rows),
            "self_citations_indexed": indexed,
            "relationships": relationships,
            "unresolved": unresolved,
        }

    def _backfill_citation_index_keys(self) -> None:
        rows = self.safe_execute(
            "SELECT citation, doc_id, citation_key FROM citation_index"
        ).fetchall()
        updated = False
        for citation, doc_id, citation_key in rows:
            normalized = normalize_citation(citation)
            if normalized and citation_key != normalized:
                self.safe_execute(
                    "UPDATE citation_index SET citation_key=? WHERE citation=? AND doc_id=?",
                    (normalized, citation, doc_id),
                )
                updated = True
        if updated:
            self.safe_commit()

    @staticmethod
    def _derive_case_year(barcode: str | None, entities_json: str | None) -> str | None:
        parts = (barcode or "").split("-", 5)
        if len(parts) == 6 and parts[4].isdigit() and parts[4] != "0000":
            return parts[4]
        try:
            entities = json.loads(entities_json) if entities_json else {}
        except Exception:
            entities = {}
        dates = entities.get("DATE") if isinstance(entities, dict) else None
        if isinstance(dates, list):
            for raw in dates:
                for token in str(raw).split():
                    if token.isdigit() and len(token) == 4:
                        return token
        return None

    # ---------- full-text search (FTS5) ----------
    def fts_search(self, query: str, limit: int = 50) -> list[dict]:
        """Fast full-text search using SQLite FTS5.

        Falls back to a LIKE scan on the first 2 000 rows if the FTS5
        virtual table is not available (e.g. older SQLite build).

        Returns a list of dicts with keys:
            id, ref_no, virtual_folder, source_url, barcode,
            barcode_confidence, snippet
        """
        # Sanitise the query so special FTS5 operators don't crash it
        safe_query = query.replace('"', '""').strip()

        try:
            rows = self.safe_execute(
                """SELECT d.id, d.ref_no, d.virtual_folder, d.source_url,
                          d.barcode, d.barcode_confidence,
                          snippet(documents_fts, 4, '<b>', '</b>', '…', 32) AS snip
                   FROM documents_fts
                   JOIN documents d ON d.id = documents_fts.id
                   WHERE documents_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (safe_query, limit),
            ).fetchall()
        except Exception:
            # FTS5 not available — fall back to basic LIKE scan
            # Escape SQL LIKE special characters in the original query
            like_safe = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{like_safe}%"
            rows = self.safe_execute(
                """SELECT id, ref_no, virtual_folder, source_url,
                          barcode, barcode_confidence,
                          SUBSTR(text, 1, 300) AS snip
                   FROM documents
                   WHERE text LIKE ? ESCAPE '\\' OR keywords_json LIKE ? ESCAPE '\\'
                   ORDER BY added_at DESC
                   LIMIT ?""",
                (like, like, limit),
            ).fetchall()

        return [
            {
                "id": r[0],
                "ref_no": r[1],
                "virtual_folder": r[2],
                "source_url": r[3],
                "barcode": r[4],
                "barcode_confidence": r[5],
                "snippet": (r[6] or "").replace("\n", " ")[:400],
            }
            for r in rows
        ]

    # ---------- ingestion jobs ----------
    def upsert_ingestion_job(
        self,
        *,
        job_id: str,
        source_path: str,
        state: str,
        source_url: str | None = None,
        doc_id: str | None = None,
        content_hash: str | None = None,
        source_fingerprint: str | None = None,
        source_mtime: str | None = None,
        quarantined_path: str | None = None,
        error_type: str | None = None,
        error_details: str | None = None,
        increment_attempt: bool = False,
    ) -> None:
        current_attempts = 0
        existing = self.safe_execute(
            "SELECT attempts FROM ingestion_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if existing:
            current_attempts = int(existing[0] or 0)
        attempts = current_attempts + 1 if increment_attempt else current_attempts
        self.safe_execute(
            """INSERT INTO ingestion_jobs
               (job_id, source_path, source_url, state, doc_id, content_hash,
                source_fingerprint, source_mtime, attempts, quarantined_path,
                error_type, error_details, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(job_id) DO UPDATE SET
                 source_path=excluded.source_path,
                 source_url=excluded.source_url,
                 state=excluded.state,
                 doc_id=excluded.doc_id,
                 content_hash=excluded.content_hash,
                 source_fingerprint=excluded.source_fingerprint,
                 source_mtime=excluded.source_mtime,
                 attempts=excluded.attempts,
                 quarantined_path=excluded.quarantined_path,
                 error_type=excluded.error_type,
                 error_details=excluded.error_details,
                 updated_at=CURRENT_TIMESTAMP
            """,
            (
                job_id,
                source_path,
                source_url,
                state,
                doc_id,
                content_hash,
                source_fingerprint,
                source_mtime,
                attempts,
                quarantined_path,
                error_type,
                error_details,
            ),
        )
        self.safe_commit()

    def get_ingestion_job(self, job_id: str) -> dict | None:
        row = self.safe_execute(
            """SELECT job_id, source_path, source_url, state, doc_id, content_hash,
                      source_fingerprint, source_mtime, attempts, quarantined_path,
                      error_type, error_details, created_at, updated_at
               FROM ingestion_jobs WHERE job_id = ?""",
            (job_id,),
        ).fetchone()
        if not row:
            return None
        keys = [
            "job_id", "source_path", "source_url", "state", "doc_id", "content_hash",
            "source_fingerprint", "source_mtime", "attempts", "quarantined_path",
            "error_type", "error_details", "created_at", "updated_at",
        ]
        return dict(zip(keys, row))

    def list_ingestion_jobs(self, state: str | None = None, limit: int = 100) -> list[dict]:
        params: tuple = ()
        query = """SELECT job_id, source_path, source_url, state, doc_id, attempts,
                          quarantined_path, error_type, error_details, updated_at
                   FROM ingestion_jobs"""
        if state:
            query += " WHERE state = ?"
            params = (state,)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params = params + (limit,)
        rows = self.safe_execute(query, params).fetchall()
        return [
            {
                "job_id": r[0],
                "source_path": r[1],
                "source_url": r[2],
                "state": r[3],
                "doc_id": r[4],
                "attempts": r[5],
                "quarantined_path": r[6],
                "error_type": r[7],
                "error_details": r[8],
                "updated_at": r[9],
            }
            for r in rows
        ]

    # ---------- backup history ----------
    def record_backup(self, backup_id: str, archive_path: str, verified_ok: bool, details: dict | None = None) -> None:
        self.safe_execute(
            """INSERT OR REPLACE INTO backup_history
               (backup_id, archive_path, verified_ok, details_json, created_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (backup_id, archive_path, 1 if verified_ok else 0, json.dumps(details or {})),
        )
        self.safe_commit()

    def list_backups(self, limit: int = 20) -> list[dict]:
        rows = self.safe_execute(
            """SELECT backup_id, archive_path, verified_ok, details_json, created_at
               FROM backup_history ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            {
                "backup_id": r[0],
                "archive_path": r[1],
                "verified_ok": bool(r[2]),
                "details": json.loads(r[3]) if r[3] else {},
                "created_at": r[4],
            }
            for r in rows
        ]