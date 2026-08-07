"""Main pipeline (metadata-only mode):
extract -> analyze/cross-ref -> tag -> ID -> glue (DB, full text + tags) 
-> categorize (virtual folder) -> delete original if a repull source is known.
"""
import time
import random
from collections import deque
import logging
import json  # ✅ Fixed missing import used by sidecar extraction
import sys
import os
from pathlib import Path

import traceback
from datetime import datetime

def log_ui_error(file_path: str, error_type: str, details: str, source_url: str = None):
    """Writes a structured error log that the front-end UI can easily read and display."""
    ui_log_path = Path("ui_extraction_errors.json")
    
    error_entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file_name": Path(file_path).name,
        "error_type": error_type,
        "details": details,
        "source_url": source_url or "None provided"
    }
    
    # Load existing logs or start a new list
    current_logs = []
    if ui_log_path.exists():
        try:
            current_logs = json.loads(ui_log_path.read_text(encoding="utf-8"))
        except Exception:
            pass # Failsafe if the file gets corrupted
            
    current_logs.append(error_entry)
    ui_log_path.write_text(json.dumps(current_logs, indent=4), encoding="utf-8")
    log.error(f"[UI-FLAGGED ERROR] {error_type} in {Path(file_path).name}: {details}")

from . import config as cfgmod
from . import extractor
from . import tagger
from . import organizer
from . import analyzer 
from .database import DB

log = logging.getLogger(__name__)

# Global state for API Keys
# Schema: { "token_string": { "enabled": True, "cooldown_until": 0, "consecutive_429s": 0 } }
TOKEN_REGISTRY = {}

# Engine Safety Limits
MAX_RUN_TIME_MINUTES = 30  
ENGINE_START_TIME = time.time()
RECENT_LOGS = deque(maxlen=5) 

def initialize_token_registry(cfg: dict):
    """Loads tokens from config into the registry on startup."""
    global TOKEN_REGISTRY
    if TOKEN_REGISTRY: 
        return  
        
    cl_cfg = cfg.get("courtlistener", {})
    tokens = cl_cfg.get("api_tokens", [])
    if not tokens and cl_cfg.get("api_token"): 
        tokens = [cl_cfg.get("api_token")]
        
    for t in tokens:
        TOKEN_REGISTRY[t] = {
            "enabled": True, 
            "cooldown_until": 0,
            "consecutive_429s": 0
        }

# Global cache for instant deduplication lookups
LOCAL_DOC_CACHE = set()

def initialize_document_cache(db):
    """Loads all existing local document IDs into RAM for O(1) instant lookup."""
    global LOCAL_DOC_CACHE
    if LOCAL_DOC_CACHE:
        return # Cache is already built
        
    try:
        cursor = db.cursor() if hasattr(db, 'cursor') else db.conn.cursor()
        cursor.execute("SELECT id FROM documents")
        LOCAL_DOC_CACHE = set(row[0] for row in cursor.fetchall())
        log.info(f"[SYSTEM] Deduplication shield activated. {len(LOCAL_DOC_CACHE)} unique keys cached in RAM.")
    except Exception as e:
        log.error(f"[-] Failed to initialize document cache shield: {e}")

def should_stop_engine(current_log_msg: str) -> tuple[bool, str]:
    """Evaluates runtime limits and stuck-loops."""
    elapsed_minutes = (time.time() - ENGINE_START_TIME) / 60
    if elapsed_minutes >= MAX_RUN_TIME_MINUTES:
        return True, f"Duration timer reached ({MAX_RUN_TIME_MINUTES} mins elapsed)."

    RECENT_LOGS.append(current_log_msg)
    if len(RECENT_LOGS) == RECENT_LOGS.maxlen and len(set(RECENT_LOGS)) == 1:
        return True, "Infinite loop safety triggered: Same log pattern repeated."

    return False, ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("legal_sorter")

SUPPORTED_EXTS = {".pdf", ".docx", ".html", ".htm", ".webarchive", ".mhtml", ".txt", ".md"}

# Global pointer for Round-Robin API Key usage
_CURRENT_TOKEN_INDEX = 0


def is_valid_pdf(file_path: Path) -> bool:
    """Verifies if a downloaded file contains true PDF magic binary headers."""
    if file_path.suffix.lower() != ".pdf":
        return True
    if not file_path.exists() or file_path.stat().st_size < 4:
        return False
    try:
        with open(file_path, 'rb') as f:
            return f.read(4) == b'%PDF'
    except Exception:
        return False


def _load_sidecar_url(file_path: str) -> str | None:
    """Reads the sidecar to get the source_url, but leaves it intact in case of a crash."""
    sidecar = Path(str(file_path) + ".meta.json")
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            return data.get("source_url")
        except Exception:
            return None
    return None

def process_file(file_path: str, cfg: dict, db: DB):
    p = Path(file_path)
    if p.suffix.lower() not in SUPPORTED_EXTS:
        return

    if p.suffix.lower() == ".pdf" and not is_valid_pdf(p):
        log.warning(f"[GUARDIAN] {p.name} is a fake PDF/API error block. Purging physical file.")
        p.unlink(missing_ok=True)
        return

    doc_id = organizer.compute_id(file_path)
    if db.get_document(doc_id):
        log.info(f"Already processed (dedup): {p.name} -> {doc_id[:12]}")
        p.unlink(missing_ok=True)
        Path(str(file_path) + ".meta.json").unlink(missing_ok=True)
        return

    source_url = _load_sidecar_url(file_path)

    # --- ERR MARKER 1: FILE TOO BIG ---
    MAX_FILE_SIZE_MB = 30
    if p.stat().st_size > (MAX_FILE_SIZE_MB * 1024 * 1024):
        log_ui_error(str(p), "File Too Large", f"File exceeds {MAX_FILE_SIZE_MB}MB safety limit.", source_url)
        p.unlink(missing_ok=True)
        return

    text_content = ""
    extracted_file_type = "pdf"
    
    # --- ERR MARKER 2: EXTRACTION CRASH ---
    try:
        extracted = extractor.extract(file_path)
        text_content = extracted.text
        extracted_file_type = extracted.file_type
        if not source_url:
            source_url = extracted.source_url
            
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc().strip().split('\n')[-1]
        log_ui_error(str(p), "Extraction Crash", f"{str(e)} | Trace: {error_trace}", source_url)
        p.unlink(missing_ok=True) 
        Path(str(file_path) + ".meta.json").unlink(missing_ok=True)
        return

    # --- ERR MARKER 3: DEAD END (No Text + No URL) OR STUB ---
    if not text_content.strip():
        if not source_url:
            # Complete failure: unreadable and no link to fallback on
            log_ui_error(str(p), "Dead End Document", "Document contains no readable text and lacks a source URL for manual review.", source_url)
            p.unlink(missing_ok=True)
            Path(str(file_path) + ".meta.json").unlink(missing_ok=True)
            return
            
        else:
            # Unreadable, but we have a URL: Proceed with Virtual Stub
            log.warning(f"[STUB] {p.name} is encrypted or unreadable. Virtualizing link and purging physical file.")

            virtual_folder = "System_Alerts/Unreadable_Documents"
            stub_text = f"[UNREADABLE DOCUMENT]\nThis file was either heavily encrypted by the court or contained no extractable text. View manually at: {source_url}"

            db.insert_document(
                doc_id, str(p), "pdf",
                {"RULING_LOGIC": "Unreadable - Requires Manual URL Visit"}, [], ["Unreadable", "Manual Review Required"],
                stub_text, source_url=source_url, virtual_folder=virtual_folder
            )
     
            try:
                base_archive = Path(cfg.get("archive_folder", "Case_Library")).resolve()
                archive_dir = base_archive / virtual_folder
                archive_dir.mkdir(parents=True, exist_ok=True)
                brief_path = archive_dir / f"brief_{doc_id[:12]}.md"
                brief_path.write_text(f"# Unreadable File Link\n**Case ID:** {doc_id}\n**Access Document Manually:** {source_url}", encoding="utf-8")
            except Exception:
                pass

            p.unlink(missing_ok=True)
            Path(str(file_path) + ".meta.json").unlink(missing_ok=True)
            return

    # [The rest of your successful text processing logic continues below here...]

    log.info(f"[*] Analyzing ruling logic and scanning citations for: {p.name}")
    analysis = analyzer.analyze_document_text(text_content)

    corpus = db.all_texts_except(doc_id)
    max_kw = cfg.get("tagging", {}).get("max_keywords_per_doc", 10)
    tags = tagger.tag_document(text_content, corpus, max_kw)

    tags.entities["RULING_LOGIC"] = analysis.ruling_logic
    tags.citations = sorted(list(set(tags.citations + analysis.citations)))
    tags.keywords = sorted(list(set(tags.keywords + analysis.suggested_tags)))

    self_citation = tagger.extract_self_citation(text_content, tags.citations)

    for citation in tags.citations:
        if citation == self_citation:
            continue
        existing_doc_id = db.lookup_citation(citation)
        if existing_doc_id and existing_doc_id != doc_id:
            db.add_cross_reference(doc_id, existing_doc_id, citation)
            log.info(f"  [x-ref] This case cites an already-indexed case: {citation}")
        elif not db.check_citation_indexed(citation):
            db.add_to_priority_queue(citation, doc_id)

    virtual_folder = organizer.build_virtual_folder(tags.entities, tags.citations, tags.keywords)

    db.insert_document(
        doc_id, str(p), extracted_file_type,
        tags.entities, tags.citations, tags.keywords, text_content,
        source_url=source_url, virtual_folder=virtual_folder,
    )

    ref_no = db.assign_ref_no(doc_id)
    if self_citation:
        db.register_self_citation(self_citation, doc_id)
    log.info(f"  [#] Assigned reference number: {ref_no}")

    archive_success = False
    try:
        clean_virtual = (virtual_folder or "Uncategorized").replace("\\\\", "/").replace("\\", "/")
        base_archive = Path(cfg.get("archive_folder", "Case_Library")).resolve()
        archive_dir = base_archive / clean_virtual
        archive_dir.mkdir(parents=True, exist_ok=True)

        bt = "```"
        brief_path = archive_dir / f"brief_{doc_id[:12]}.md"
        ruling_logic = tags.entities.get("RULING_LOGIC", "No explicit ruling logic extracted.")

        brief_content = f"""# Case Brief Profile: {doc_id[:12]}\n**Reference Number:** {ref_no}\n**Virtual Path:** `{clean_virtual}`\n**Source URL:** {source_url or 'None provided'}\n\n## Extracted Verbatim Ruling Logic\n> {ruling_logic.strip()}\n\n## Metadata & Tags\n* **Keywords:** {', '.join(tags.keywords) if tags.keywords else 'None'}\n* **Cross-References:** {', '.join(tags.citations) if tags.citations else 'None'}\n\n---\n## Preview (First 500 Characters)\n{bt}text\n{text_content[:500].strip()}...\n{bt}\n"""
        brief_path.write_text(brief_content, encoding="utf-8")

        log.info(f"  [+] Brief written -> {brief_path.relative_to(Path.cwd())} (no raw file duplicated)")
        archive_success = True

    except Exception as archive_err:
        log.error(f"  [-] Failed to write brief file: {archive_err}")

    if archive_success:
        p.unlink(missing_ok=True)
        Path(str(file_path) + ".meta.json").unlink(missing_ok=True)
        db.mark_deleted_original(doc_id)
        log.info(f"Done: {p.name} -> {ref_no} [{virtual_folder}] (metadata indexed, original deleted)")


def scan_once(cfg: dict, db: DB):
    pull_folder = Path(cfg["pull_folder"])
    if not pull_folder.exists():
        return
    for f in pull_folder.iterdir():
        if f.is_file() and not f.name.endswith(".meta.json") and f.parent.name != "manual_review":
            process_file(str(f), cfg, db)


def auto_feed_queue(cfg: dict, db: DB):
    global TOKEN_REGISTRY, LOCAL_DOC_CACHE
    initialize_token_registry(cfg)
    initialize_document_cache(db)

    citation = db.get_next_priority_citation()
    if not citation: 
        return

    if citation in LOCAL_DOC_CACHE:
        log.info(f"[Shield] Skipping {citation} — already exists in local archive.")
        db.mark_priority_fetched(citation)
        return

    should_stop, reason = should_stop_engine(f"Target: {citation}")
    if should_stop:
        log.error(f"[-] Engine Auto-Halted: {reason}")
        return

    cl_cfg = cfg.get("courtlistener", {})
    token_pool = cl_cfg.get("api_tokens", [])
    if not token_pool and cl_cfg.get("api_token"):
        token_pool = [cl_cfg.get("api_token")]

    if not token_pool:
        log.error("[-] Engine Halt: No active CourtListener API tokens found.")
        return

    db.mark_priority_failed(citation)
    tried_tokens = set()

    for attempt in range(len(token_pool)):
        available_tokens = [t for t in token_pool if t not in tried_tokens]
        if not available_tokens:
            break

        active_token = random.choice(available_tokens)
        tried_tokens.add(active_token)

        masked_sig = f"...{active_token[-6:]}"
        log.info(f"[*] Auto-Feeding: Attempt {attempt + 1}. Trying Key: ({masked_sig}) -> Target: {citation}")

        try:
            from .legal_fetch import CourtListenerClient
            client = CourtListenerClient(active_token, cl_cfg.get("base_url"))

            saved = client.pull_into_folder(citation, cfg["pull_folder"], db=db)

            if saved:
                log.info(f"  [+] Automatically retrieved: {citation}")
                db.mark_priority_fetched(citation)
                
                LOCAL_DOC_CACHE.add(citation)
                
                # ✅ FIX 1: Map the update back into the actual TOKEN_REGISTRY dictionary setup
                TOKEN_REGISTRY[active_token]["consecutive_429s"] = 0  
                return  
            else:
                log.warning(f"  [-] Failed to retrieve: {citation}.")
                return  
                
        except Exception as e:
            # ✅ FIX 2 & 3: Consolidated error blocks + Line/File Traceback tracking
            import traceback
            _, _, exc_tb = sys.exc_info()
            stack = traceback.extract_tb(exc_tb)
            
            file_name = "Unknown File"
            line_no = "0"
            if stack:
                last_frame = stack[-1]
                file_name = os.path.basename(last_frame.filename)
                line_no = last_frame.lineno

            pinpoint_err = f"[{file_name} Line {line_no}] -> {e}"

            if "429" in str(e):
                log.warning(f"  [!] Key ({masked_sig}) blocked (429) at {pinpoint_err}. Rotating key matrix...")
                continue  
            else:
                log.error(f"  [!] Exception occurred fetching {citation}: {pinpoint_err}")
                return  


def run_forever(poll_seconds: int = 15):
    cfg = cfgmod.load_config()
    db = DB(str(Path(cfg["index_folder"]) / "legal_sorter.db"))
    log.info(f"Watching {cfg['pull_folder']} every {poll_seconds}s. Ctrl+C to stop.")
    while True:
        scan_once(cfg, db)
        auto_feed_queue(cfg, db)
        import bulk_ingest
        bulk_ingest.run_bulk_batch(cfg, db, max_items=50) 
        time.sleep(poll_seconds)