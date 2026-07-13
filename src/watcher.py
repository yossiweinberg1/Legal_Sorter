"""Main pipeline (metadata-only mode):
extract -> analyze/cross-ref -> tag -> ID -> glue (DB, full text + tags) 
-> categorize (virtual folder) -> delete original if a repull source is known.
"""
import time
import random
from collections import deque
import logging

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
        # Assuming your db object exposes a cursor or connection method
        # Adjust the query slightly if your table uses 'id' instead of 'citation'
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

import os
from pathlib import Path

from . import config as cfgmod
from . import extractor
from . import tagger
from . import organizer
from . import analyzer 
from .database import DB

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
    """legal_fetch.py writes a <file>.meta.json sidecar with source_url for
    anything pulled from CourtListener. If present, use it and clean it up."""
    sidecar = Path(str(file_path) + ".meta.json")
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            sidecar.unlink()
            return data.get("source_url")
        except Exception:
            return None
    return None


def process_file(file_path: str, cfg: dict, db: DB):
    p = Path(file_path)
    if p.suffix.lower() not in SUPPORTED_EXTS:
        return

    # =========================================================================
    # GUARDIAN UPGRADE: CATCH FAKE PDFS
    # =========================================================================
    if p.suffix.lower() == ".pdf" and not is_valid_pdf(p):
        log.warning(f"[GUARDIAN] {p.name} is a fake PDF/API error block. Purging physical file.")
        p.unlink(missing_ok=True)
        return

    # 1. ID first -- also gives us free dedup
    doc_id = organizer.compute_id(file_path)
    if db.get_document(doc_id):
        log.info(f"Already processed (dedup): {p.name} -> {doc_id[:12]}")
        p.unlink(missing_ok=True)
        return

    # Grab source URL early so we have it even if extraction fails
    source_url = _load_sidecar_url(file_path)

    # 2. Extract with Ghost Stub Logic
    text_content = ""
    extracted_file_type = "pdf"
    try:
        extracted = extractor.extract(file_path)
        text_content = extracted.text
        extracted_file_type = extracted.file_type
        if not source_url:
            source_url = extracted.source_url
    except Exception as e:
        pass  # We catch the error silently and let the Ghost Stub logic handle it below

    # If the file crashed the extractor OR has no text, create a Ghost Stub and DELETE the file.
    if not text_content.strip():
        log.warning(f"[STUB] {p.name} is encrypted or unreadable. Virtualizing link and purging physical file.")

        virtual_folder = "System_Alerts/Unreadable_Documents"
        stub_text = f"[UNREADABLE DOCUMENT]\nThis file was either heavily encrypted by the court or contained no extractable text. View manually at: {source_url}"

        # Save to DB so we never try to download it again
        db.insert_document(
            doc_id, str(p), "pdf",
            {"RULING_LOGIC": "Unreadable - Requires Manual URL Visit"}, [], ["Unreadable", "Manual Review Required"],
            stub_text, source_url=source_url, virtual_folder=virtual_folder
        )

        # Build the physical folder tree and drop a brief with the link
        try:
            base_archive = Path(cfg.get("archive_folder", "Case_Library")).resolve()
            archive_dir = base_archive / virtual_folder
            archive_dir.mkdir(parents=True, exist_ok=True)
            brief_path = archive_dir / f"brief_{doc_id[:12]}.md"
            brief_path.write_text(f"# Unreadable File Link\n**Case ID:** {doc_id}\n**Access Document Manually:** {source_url}", encoding="utf-8")
        except Exception:
            pass

        # DESTROY THE ORIGINAL FILE TO CLEAR THE QUEUE
        p.unlink(missing_ok=True)
        return

    # --- NORMAL FILE PROCESSING CONTINUES HERE ---
    log.info(f"[*] Analyzing ruling logic and scanning citations for: {p.name}")
    analysis = analyzer.analyze_document_text(text_content)

    corpus = db.all_texts_except(doc_id)
    max_kw = cfg.get("tagging", {}).get("max_keywords_per_doc", 10)
    tags = tagger.tag_document(text_content, corpus, max_kw)

    tags.entities["RULING_LOGIC"] = analysis.ruling_logic
    tags.citations = sorted(list(set(tags.citations + analysis.citations)))
    tags.keywords = sorted(list(set(tags.keywords + analysis.suggested_tags)))

    # ---- Cross-referencing: link to already-indexed cases, or queue missing ones ----
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

        # Only the lightweight brief (tags + a short excerpt) is kept on disk.
        # The full document text already lives in the database (searchable via
        # `python run.py search ...`), and the original is re-pullable via
        # source_url -- so there is no need to duplicate the whole file here.
        brief_content = f"""# Case Brief Profile: {doc_id[:12]}\n**Reference Number:** {ref_no}\n**Virtual Path:** `{clean_virtual}`\n**Source URL:** {source_url or 'None provided'}\n\n## Extracted Verbatim Ruling Logic\n> {ruling_logic.strip()}\n\n## Metadata & Tags\n* **Keywords:** {', '.join(tags.keywords) if tags.keywords else 'None'}\n* **Cross-References:** {', '.join(tags.citations) if tags.citations else 'None'}\n\n---\n## Preview (First 500 Characters)\n{bt}text\n{text_content[:500].strip()}...\n{bt}\n"""
        brief_path.write_text(brief_content, encoding="utf-8")

        log.info(f"  [+] Brief written -> {brief_path.relative_to(Path.cwd())} (no raw file duplicated)")
        archive_success = True

    except Exception as archive_err:
        log.error(f"  [-] Failed to write brief file: {archive_err}")

    if archive_success:
        p.unlink(missing_ok=True)
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
    
    # 1. Spin up the RAM shield if it isn't running yet
    initialize_document_cache(db)

    citation = db.get_next_priority_citation()
    if not citation: 
        return

    # 2. THE SHIELD GATEKEEPER: Check if we already have this case
    if citation in LOCAL_DOC_CACHE:
        log.info(f"[Shield] Skipping {citation} — already exists in local archive.")
        db.mark_priority_fetched(citation) # Clear it out of your queue table quietly
        return

    # 3. Check Safety Limits (Timer & Loops)
    should_stop, reason = should_stop_engine(f"Target: {citation}")
    if should_stop:
        log.error(f"[-] Engine Auto-Halted: {reason}")
        return

    # ... Rest of your existing API request / courtlistener client logic continues below ...

    # Load configuration
    cl_cfg = cfg.get("courtlistener", {})
    token_pool = cl_cfg.get("api_tokens", [])
    if not token_pool and cl_cfg.get("api_token"):
        token_pool = [cl_cfg.get("api_token")]

    if not token_pool:
        log.error("[-] Engine Halt: No active CourtListener API tokens found.")
        return

    # Initial Pessimistic Lock
    db.mark_priority_failed(citation)

    # Tracking for randomized retries
    tried_tokens = set()

    # Retry logic: attempt up to the number of tokens you have
    for attempt in range(len(token_pool)):
        # Filter pool to tokens we haven't tried yet for this citation
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
                
                # Success: Shield this citation in RAM so neither API nor BULK engines touch it again
                LOCAL_DOC_CACHE.add(citation)
                
                # Reset consecutive errors for this API token since it worked
                meta["consecutive_429s"] = 0  
                return  # Success! Exit function
            else:
                # API didn't fetch it, so it is NOT added to LOCAL_DOC_CACHE
                log.warning(f"  [-] Failed to retrieve: {citation}.")
                return  # Move on
                
        except Exception as e:
            # Catch network errors/timeouts so the engine marks it as a failure but stays alive
            log.error(f"  [!] Exception occurred fetching {citation}: {e}")
            return  # Failsafe move on; leaves citation free for a later retry

        except Exception as e:
            if "429" in str(e):
                log.warning(f"  [!] Key ({masked_sig}) blocked (429). Rotating...")
                continue  # Try the next random token
            else:
                log.error(f"  [-] Fatal Error: {e}")
                return  # Stop processing this citation on critical error


def run_forever(poll_seconds: int = 15):
    cfg = cfgmod.load_config()
    db = DB(str(Path(cfg["index_folder"]) / "legal_sorter.db"))
    log.info(f"Watching {cfg['pull_folder']} every {poll_seconds}s. Ctrl+C to stop.")
    while True:
        # Step A: Scan and index any files manually dropped into the folder
        scan_once(cfg, db)

        # Step B: Check the queue and pull 1 missing cross-referenced case from the web
        auto_feed_queue(cfg, db)
        import bulk_ingest
        bulk_ingest.run_bulk_batch(cfg, db, max_items=50) 
        time.sleep(poll_seconds)