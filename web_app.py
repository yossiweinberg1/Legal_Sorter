"""Web interface for the Legal Sorter archive.

Exposes the indexed legal case database as a searchable website with
bulk-ingest support (paste raw legal text → dedup → analyze → store).

Run:
    uvicorn web_app:app --host 0.0.0.0 --port 8000

Then open http://localhost:8000 in your browser.

Configuration:
    The app reads config.yaml for the database path.
    Set LLM_API_KEY in your environment to enable AI-powered Q&A.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from src import config as cfgmod
from src import audit as auditlog
from src.database import connect_sqlite, resolve_db_path
from src.icon_utils import logo_data_uri

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Legal Sorter — Case Archive",
    description="Search, AI Q&A, and bulk-ingest interface for indexed legal cases.",
    version="1.1.0",
)

log = logging.getLogger(__name__)


def _db_path() -> str:
    return resolve_db_path()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str
    top_k: int = 5


class IngestRequest(BaseModel):
    text: str
    label: str = ""  # optional human-readable label / case name


ROLE_ORDER = {"anonymous": 0, "reader": 1, "operator": 2, "admin": 3}


def _cfg() -> dict:
    return cfgmod.load_config()


def _principal(required_role: str):
    def dependency(x_api_key: str | None = Header(default=None, alias="X-API-Key")):
        cfg = _cfg()
        auth_cfg = cfg.get("auth", {})
        if not auth_cfg.get("enabled"):
            return {"actor": "anonymous", "role": "anonymous"}
        for entry in auth_cfg.get("api_keys", []):
            if x_api_key and x_api_key == entry.get("key"):
                role = str(entry.get("role", "reader"))
                if ROLE_ORDER.get(role, 0) < ROLE_ORDER.get(required_role, 0):
                    raise HTTPException(status_code=403, detail="Insufficient role for this endpoint.")
                return {"actor": f"api_key:{role}", "role": role}
        raise HTTPException(status_code=401, detail="Missing or invalid API key.")
    return dependency


require_reader = _principal("reader")
require_operator = _principal("operator")
require_admin = _principal("admin")


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/api/search", tags=["Search"])
def search(
    q: str = Query(..., description="Search query — supports FTS5 phrases and AND/OR"),
    limit: int = Query(default=20, le=100),
    principal: dict = Depends(require_reader),
):
    """Full-text search across all indexed cases.

    Uses SQLite FTS5 when available, falls back to LIKE scan.
    Returns a list of matching case summaries.
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query must not be empty.")

    try:
        from src.database import DB
        db = DB(_db_path())
        results = db.fts_search(q.strip(), limit=limit)
        history_map = db.get_subsequent_history_summary_map([item["id"] for item in results], limit=3)
        for item in results:
            summary = history_map.get(item["id"], {"count": 0, "items": []})
            item["subsequent_history"] = summary["items"]
            item["subsequent_history_count"] = summary["count"]
        auditlog.log_event(_cfg(), "api.search", actor=principal["actor"], role=principal["role"], details={"query": q.strip(), "limit": limit, "count": len(results)})
        return {"query": q, "count": len(results), "results": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/case/{doc_id}", tags=["Cases"])
def get_case(doc_id: str, principal: dict = Depends(require_reader)):
    """Retrieve full metadata and text for a single indexed case."""
    db = None
    try:
        from src.database import DB
        db = DB(_db_path())
        row = db.safe_execute(
            """SELECT id, ref_no, virtual_folder, source_url, file_type,
                      entities_json, citations_json, keywords_json,
                      added_at, barcode, barcode_strategy,
                      barcode_confidence, barcode_confirmed, text
               FROM documents WHERE id = ?""",
            (doc_id,),
        ).fetchone()
        cited_cases = db.get_cross_references(doc_id)
        subsequent_history = db.get_subsequent_history(doc_id, limit=20)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if db is not None:
            db.conn.close()

    if not row:
        raise HTTPException(status_code=404, detail=f"Case {doc_id[:12]} not found.")

    def _j(v):
        try:
            return json.loads(v) if v else []
        except Exception:
            return []

    full_text = row[13] or ""
    full_text_cap = 12000
    result = {
        "id": row[0],
        "ref_no": row[1],
        "virtual_folder": row[2],
        "source_url": row[3],
        "file_type": row[4],
        "entities": _j(row[5]),
        "citations": _j(row[6]),
        "keywords": _j(row[7]),
        "preview": (full_text[:8000]).replace("\n", " ").strip(),
        "added_at": row[8],
        "barcode": row[9],
        "barcode_strategy": row[10],
        "barcode_confidence": row[11],
        "barcode_confirmed": bool(row[12]) if row[12] is not None else False,
        "full_text": full_text[:full_text_cap],
        "full_text_truncated": len(full_text) > full_text_cap,
        "cited_cases": cited_cases,
        "subsequent_history": subsequent_history,
    }
    auditlog.log_event(_cfg(), "api.case", actor=principal["actor"], role=principal["role"], details={"doc_id": doc_id, "ref_no": row[1]})
    return result


@app.get("/api/case/{doc_id}/subsequent_history", tags=["Cases"])
def get_case_subsequent_history(
    doc_id: str,
    limit: int = Query(default=20, le=100),
    principal: dict = Depends(require_reader),
):
    db = None
    try:
        from src.database import DB
        db = DB(_db_path())
        items = db.get_subsequent_history(doc_id, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        if db is not None:
            db.conn.close()
    auditlog.log_event(_cfg(), "api.case_subsequent_history", actor=principal["actor"], role=principal["role"], details={"doc_id": doc_id, "limit": limit, "count": len(items)})
    return {"doc_id": doc_id, "count": len(items), "results": items}


@app.get("/api/cases", tags=["Cases"])
def list_cases(
    folder: Optional[str] = Query(default=None, description="Filter by virtual_folder prefix"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    principal: dict = Depends(require_reader),
):
    """List indexed cases, optionally filtered by virtual folder."""
    try:
        from src.database import DB
        db = DB(_db_path())
        try:
            history_map = {}
            with connect_sqlite(_db_path()) as conn:
                if folder:
                    rows = conn.execute(
                        """SELECT id, ref_no, virtual_folder, source_url, added_at,
                                  barcode, barcode_confidence
                           FROM documents WHERE virtual_folder LIKE ?
                           ORDER BY added_at DESC LIMIT ? OFFSET ?""",
                        (f"{folder}%", limit, offset),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT id, ref_no, virtual_folder, source_url, added_at,
                                  barcode, barcode_confidence
                           FROM documents ORDER BY added_at DESC LIMIT ? OFFSET ?""",
                        (limit, offset),
                    ).fetchall()
            history_map = db.get_subsequent_history_summary_map([r[0] for r in rows], limit=3)
        finally:
            db.conn.close()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result = {
        "count": len(rows),
        "offset": offset,
        "results": [
            {
                "id": r[0],
                "ref_no": r[1],
                "virtual_folder": r[2],
                "source_url": r[3],
                "added_at": r[4],
                "barcode": r[5],
                "barcode_confidence": r[6],
                "subsequent_history": history_map.get(r[0], {}).get("items", []),
            }
            for r in rows
        ],
    }
    auditlog.log_event(_cfg(), "api.cases", actor=principal["actor"], role=principal["role"], details={"folder": folder, "limit": limit, "offset": offset, "count": len(rows)})
    return result


@app.post("/api/ask", tags=["AI"])
def ask(body: AskRequest, principal: dict = Depends(require_reader)):
    """Ask a legal question. The AI answers using only the indexed cases as context.

    Requires LLM_API_KEY environment variable (or a local Ollama endpoint in config.yaml).
    """
    try:
        from src.legal_ai import query_cases
        answer, sources = query_cases(_db_path(), body.question, top_k=body.top_k)
        auditlog.log_event(_cfg(), "api.ask", actor=principal["actor"], role=principal["role"], details={"question": body.question[:250], "top_k": body.top_k, "sources": len(sources)})
        return {
            "answer": answer,
            "sources": sources,
            "diagnostics": {
                "grounded_source_count": len(sources),
                "grounded": "[SOURCE " in answer,
            },
        }
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"LLM error: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/stats", tags=["Info"])
def stats(principal: dict = Depends(require_reader)):
    """Return basic statistics about the indexed archive."""
    try:
        with connect_sqlite(_db_path()) as conn:
            total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            folders = conn.execute(
                "SELECT virtual_folder, COUNT(*) FROM documents GROUP BY virtual_folder ORDER BY 2 DESC LIMIT 20"
            ).fetchall()
            latest = conn.execute(
                "SELECT ref_no, virtual_folder, added_at FROM documents ORDER BY added_at DESC LIMIT 5"
            ).fetchall()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result = {
        "total_cases": total,
        "top_folders": [{"folder": r[0], "count": r[1]} for r in folders],
        "recently_added": [{"ref_no": r[0], "folder": r[1], "added_at": r[2]} for r in latest],
    }
    auditlog.log_event(_cfg(), "api.stats", actor=principal["actor"], role=principal["role"], details={"total_cases": total})
    return result


# ---------------------------------------------------------------------------
# Bulk-ingest endpoint
# ---------------------------------------------------------------------------

@app.post("/api/ingest", tags=["Ingest"])
def ingest(body: IngestRequest, principal: dict = Depends(require_operator)):
    """Accept raw legal text (any size), run the full pipeline, and store results.

    Steps performed for each submitted blob:
      1. Content-hash the text for deterministic de-duplication.
      2. Write to a temp file so the existing ``watcher.process_file`` pipeline
         can handle extraction/analysis/tagging without duplication.
      3. Return a summary: new cases stored, duplicates skipped.

    The text may contain a single opinion or many opinions separated by blank
    lines.  The endpoint splits on 2-blank-line boundaries (common court-document
    separator) and processes each segment individually so a massive paste does
    not end up as one giant undifferentiated record.
    """
    raw = (body.text or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="text must not be empty.")

    # Split massive blobs on double-blank-line boundaries so each opinion
    # gets its own record, citations, keywords, and virtual folder.
    segments = [s.strip() for s in raw.split("\n\n\n") if s.strip()]
    if not segments:
        segments = [raw]

    cfg = _cfg()
    try:
        from src.database import DB
        from src import watcher as watchermod
        db = DB(_db_path())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database init error: {exc}") from exc

    stored: list[dict] = []
    duplicates: list[str] = []

    for seg in segments:
        content_hash = hashlib.sha256(seg.encode("utf-8")).hexdigest()
        # Use the ingest:// source_url (written to the sidecar) as the
        # canonical dedup key — this is the value that watcher.process_file
        # stores and is independent of the temp-file content hash.
        ingest_url = f"ingest://{content_hash}"

        # Check for duplicate before writing anything
        existing = db.safe_execute(
            "SELECT ref_no FROM documents WHERE source_url = ?", (ingest_url,)
        ).fetchone()
        if existing:
            duplicates.append(existing[0] or content_hash[:12])
            continue

        # Write to a temp .txt file — process_file handles all pipeline logic
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                prefix="ingest_",
                dir=cfg.get("pull_folder", "/tmp"),
                delete=False,
                encoding="utf-8",
            ) as tmp:
                tmp.write(seg)
                tmp_path = tmp.name

            # Write a minimal sidecar so the pipeline has a label and the
            # canonical source_url used for dedup above.
            label = (body.label or "").strip()[:120] or f"ingest:{content_hash[:12]}"
            sidecar_path = tmp_path + ".meta.json"
            Path(sidecar_path).write_text(
                json.dumps({"source_url": ingest_url, "case_name": label}),
                encoding="utf-8",
            )

            watchermod.process_file(tmp_path, cfg, db)

            # Retrieve the ref_no that was just assigned via the source_url key
            row = db.safe_execute(
                "SELECT ref_no, virtual_folder FROM documents WHERE source_url = ?", (ingest_url,)
            ).fetchone()
            if row:
                stored.append({"ref_no": row[0], "virtual_folder": row[1]})
            else:
                # Fallback: grab the most-recently inserted row if the
                # pipeline stored it under a different id
                row2 = db.safe_execute(
                    "SELECT ref_no, virtual_folder FROM documents ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                stored.append(
                    {"ref_no": row2[0] if row2 else "?", "virtual_folder": row2[1] if row2 else "?"}
                )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Ingest pipeline error: {exc}"
            ) from exc

    result = {
        "segments_submitted": len(segments),
        "stored": len(stored),
        "duplicates_skipped": len(duplicates),
        "new_records": stored,
        "duplicate_refs": duplicates,
    }
    auditlog.log_event(cfg, "api.ingest", actor=principal["actor"], role=principal["role"], details={"segments_submitted": len(segments), "stored": len(stored), "duplicates_skipped": len(duplicates), "label": body.label[:120]})
    return result


@app.get("/api/barcode/{barcode}", tags=["Cases"])
def get_case_by_barcode(barcode: str, principal: dict = Depends(require_reader)):
    """Retrieve a document by its structured barcode ID (LS-CT-JR-SM-YR-SQ)."""
    try:
        from src.database import DB
        db = DB(_db_path())
        doc = db.get_document_by_barcode(barcode)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not doc:
        raise HTTPException(status_code=404, detail=f"No document found with barcode {barcode}")
    auditlog.log_event(_cfg(), "api.barcode_lookup", actor=principal["actor"], role=principal["role"], details={"barcode": barcode})
    return doc


@app.post("/api/admin/regen_barcodes", tags=["Admin"])
def admin_regen_barcodes(
    force: bool = Query(default=False, description="Re-generate even confirmed barcodes"),
    min_confidence: float = Query(default=0.85, ge=0.0, le=1.0, description="Re-generate barcodes below this confidence"),
    principal: dict = Depends(require_admin),
):
    """Find and re-generate missing, failed, or low-confidence barcodes.

    Runs synchronously (suitable for small archives).  For large archives
    run ``regen_barcodes.py`` from the command line instead.
    """
    try:
        from src.database import DB
        from src import barcode as barcode_mod
        cfg = _cfg()
        db = DB(_db_path())
        docs = db.find_docs_needing_barcode_regen(min_confidence=min_confidence, force=force)
        barcode_cfg = cfg.get("barcode", {})
        confirm_threshold = float(barcode_cfg.get("confirm_threshold", 0.85))
        succeeded = 0
        failed = 0
        for doc in docs:
            try:
                import json as _json
                entities = _json.loads(doc["entities_json"]) if doc["entities_json"] else {}
                keywords = _json.loads(doc["keywords_json"]) if doc["keywords_json"] else []
                bc, strategy, confidence = barcode_mod.assign_barcode(
                    text=doc["text"] or "",
                    entities=entities,
                    citations=[],
                    keywords=keywords,
                    ref_no=doc["ref_no"] or "",
                    cfg=cfg,
                    virtual_folder=doc["virtual_folder"] or "",
                )
                db.set_barcode(
                    doc["id"], bc, strategy=strategy,
                    confidence=confidence, confirm_threshold=confirm_threshold,
                )
                succeeded += 1
            except Exception as exc:
                log.warning(
                    "regen_barcodes: failed for doc %s (ref=%s): %s",
                    doc["id"][:12], doc["ref_no"], exc,
                )
                failed += 1
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    auditlog.log_event(_cfg(), "admin.regen_barcodes", actor=principal["actor"], role=principal["role"],
                       details={"force": force, "min_confidence": min_confidence,
                                "candidates": len(docs), "succeeded": succeeded, "failed": failed})
    return {
        "candidates": len(docs),
        "succeeded": succeeded,
        "failed": failed,
    }


@app.get("/api/admin/jobs", tags=["Admin"])
def admin_jobs(
    state: Optional[str] = Query(default=None),
    limit: int = Query(default=50, le=200),
    principal: dict = Depends(require_admin),
):
    from src.database import DB
    db = DB(_db_path())
    jobs = db.list_ingestion_jobs(state=state, limit=limit)
    auditlog.log_event(_cfg(), "admin.jobs", actor=principal["actor"], role=principal["role"], details={"state": state, "limit": limit, "count": len(jobs)})
    return {"count": len(jobs), "jobs": jobs}


@app.post("/api/admin/replay/{job_id}", tags=["Admin"])
def admin_replay(job_id: str, principal: dict = Depends(require_admin)):
    from src.database import DB
    from src import watcher as watchermod
    cfg = _cfg()
    db = DB(_db_path())
    job = watchermod.replay_quarantined_job(cfg, db, job_id)
    auditlog.log_event(cfg, "admin.replay", actor=principal["actor"], role=principal["role"], details={"job_id": job_id, "state": job.get("state")})
    return job


@app.get("/api/admin/audit", tags=["Admin"])
def admin_audit(limit: int = Query(default=100, le=500), principal: dict = Depends(require_admin)):
    cfg = _cfg()
    events = auditlog.read_events(cfg, limit=limit)
    auditlog.log_event(cfg, "admin.audit", actor=principal["actor"], role=principal["role"], details={"limit": limit, "count": len(events)})
    return {"count": len(events), "events": events}


@app.get("/api/admin/backups", tags=["Admin"])
def admin_backups(limit: int = Query(default=20, le=100), principal: dict = Depends(require_admin)):
    from src.database import DB
    db = DB(_db_path())
    backups = db.list_backups(limit=limit)
    auditlog.log_event(_cfg(), "admin.backups", actor=principal["actor"], role=principal["role"], details={"limit": limit, "count": len(backups)})
    return {"count": len(backups), "backups": backups}


# ---------------------------------------------------------------------------
# Browser UI (single-page HTML)
# ---------------------------------------------------------------------------

_LOGO_DATA_URI = logo_data_uri(256)

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Legal Sorter — Archive Console</title>
<link rel="icon" type="image/png" href="__LOGO_DATA_URI__" />
<style>
  /* ── GitHub-inspired design tokens ─────────────────────────────── */
  :root {
    --color-canvas-default: #0d1117;
    --color-canvas-subtle:  #161b22;
    --color-canvas-inset:   #010409;
    --color-border-default: #30363d;
    --color-border-muted:   #21262d;
    --color-fg-default:     #e6edf3;
    --color-fg-muted:       #8b949e;
    --color-fg-subtle:      #6e7681;
    --color-accent-fg:      #58a6ff;
    --color-accent-emphasis:#1f6feb;
    --color-success-fg:     #3fb950;
    --color-danger-fg:      #f85149;
    --color-warning-fg:     #d29922;
    --color-done-fg:        #a371f7;
    --color-neutral-muted:  rgba(110,118,129,.4);
    --color-btn-bg:         #21262d;
    --color-btn-border:     rgba(240,246,252,.1);
    --color-btn-primary-bg: #238636;
    --color-btn-primary-border: rgba(240,246,252,.1);
    --shadow-small: 0 0 transparent;
    --shadow-medium: 0 3px 6px rgba(1,4,9,.4);
    --border-radius-small: 6px;
    --border-radius-medium: 6px;
    --font-mono: ui-monospace, SFMono-Regular, SF Mono, Menlo, Consolas, monospace;
  }

  *, *::before, *::after { box-sizing: border-box; }

  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    background: var(--color-canvas-default);
    color: var(--color-fg-default);
  }

  /* ── Header bar (mimics GitHub top-nav) ─────────────── */
  .gh-header {
    background: var(--color-canvas-inset);
    border-bottom: 1px solid var(--color-border-default);
    padding: 12px 16px;
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .gh-header .logo {
    text-decoration: none;
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .gh-header .logo img {
    width: 40px;
    height: 40px;
    border-radius: 8px;
    flex-shrink: 0;
  }
  .gh-header .logo span {
    color: var(--color-fg-default);
    font-size: 20px;
    font-weight: 700;
    letter-spacing: .04em;
  }
  .gh-header nav { display: flex; gap: 4px; margin-left: auto; }
  .gh-header nav a {
    color: var(--color-fg-muted);
    text-decoration: none;
    font-size: 13px;
    padding: 6px 8px;
    border-radius: var(--border-radius-small);
  }
  .gh-header nav a:hover { color: var(--color-fg-default); background: var(--color-neutral-muted); }

  /* ── Page layout ─────────────────────────────────────── */
  .gh-page { max-width: 1280px; margin: 0 auto; padding: 24px 16px; }

  /* ── Repo-style hero ─────────────────────────────────── */
  .repo-hero {
    border: 1px solid var(--color-border-default);
    border-radius: var(--border-radius-medium);
    padding: 16px;
    margin-bottom: 16px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
  }
  .repo-hero h1 { margin: 0; font-size: 20px; display: flex; align-items: center; gap: 8px; }
  .repo-hero p  { margin: 4px 0 0; color: var(--color-fg-muted); font-size: 13px; }
  .badge {
    display: inline-flex;
    align-items: center;
    padding: 2px 7px;
    font-size: 11px;
    font-weight: 500;
    border: 1px solid var(--color-border-default);
    border-radius: 2em;
    color: var(--color-fg-muted);
    background: var(--color-canvas-subtle);
  }
  .badge.green { border-color: var(--color-success-fg); color: var(--color-success-fg); }
  .badge.blue  { border-color: var(--color-accent-fg);  color: var(--color-accent-fg); }

  /* ── Tab nav (repo-file/issues style) ────────────────── */
  .gh-tabs {
    border-bottom: 1px solid var(--color-border-default);
    display: flex;
    gap: 0;
    margin-bottom: 16px;
    flex-wrap: wrap;
  }
  .gh-tab {
    padding: 8px 16px;
    font-size: 14px;
    cursor: pointer;
    border: none;
    background: none;
    color: var(--color-fg-muted);
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    display: flex;
    align-items: center;
    gap: 6px;
    transition: color .15s;
  }
  .gh-tab:hover { color: var(--color-fg-default); }
  .gh-tab.active {
    color: var(--color-fg-default);
    border-bottom-color: #f78166;
    font-weight: 600;
  }
  .gh-tab .count {
    background: var(--color-neutral-muted);
    border-radius: 2em;
    padding: 1px 6px;
    font-size: 11px;
    color: var(--color-fg-muted);
  }

  /* ── Panels ──────────────────────────────────────────── */
  .panel { display: none; }
  .panel.active { display: block; }

  /* ── Stat grid ───────────────────────────────────────── */
  .stat-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 8px;
    margin-bottom: 16px;
  }
  .stat-card {
    background: var(--color-canvas-subtle);
    border: 1px solid var(--color-border-default);
    border-radius: var(--border-radius-medium);
    padding: 14px 16px;
  }
  .stat-card .num {
    font-size: 24px;
    font-weight: 700;
    color: var(--color-accent-fg);
    line-height: 1.2;
  }
  .stat-card .lbl {
    font-size: 12px;
    color: var(--color-fg-muted);
    margin-top: 2px;
  }

  /* ── Box / Card ──────────────────────────────────────── */
  .gh-box {
    background: var(--color-canvas-subtle);
    border: 1px solid var(--color-border-default);
    border-radius: var(--border-radius-medium);
    margin-bottom: 16px;
    overflow: hidden;
  }
  .gh-box-header {
    padding: 10px 16px;
    background: var(--color-canvas-default);
    border-bottom: 1px solid var(--color-border-muted);
    font-weight: 600;
    font-size: 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .gh-box-body { padding: 16px; }

  /* ── Inputs ──────────────────────────────────────────── */
  .gh-input, .gh-textarea {
    width: 100%;
    background: var(--color-canvas-default);
    border: 1px solid var(--color-border-default);
    border-radius: var(--border-radius-small);
    color: var(--color-fg-default);
    font: inherit;
    padding: 6px 12px;
    outline: none;
    transition: border-color .15s, box-shadow .15s;
  }
  .gh-input:focus, .gh-textarea:focus {
    border-color: var(--color-accent-emphasis);
    box-shadow: 0 0 0 3px rgba(31,111,235,.4);
  }
  .gh-textarea { resize: vertical; min-height: 120px; font-size: 13px; }

  /* ── Buttons ─────────────────────────────────────────── */
  .btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 16px;
    font: inherit;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    border-radius: var(--border-radius-small);
    border: 1px solid var(--color-btn-border);
    background: var(--color-btn-bg);
    color: var(--color-fg-default);
    transition: background .15s, border-color .15s;
    white-space: nowrap;
  }
  .btn:hover { background: #30363d; }
  .btn:disabled { opacity: .6; cursor: default; }
  .btn-primary {
    background: var(--color-btn-primary-bg);
    border-color: var(--color-btn-primary-border);
    color: #fff;
  }
  .btn-primary:hover { background: #2ea043; }
  .btn-danger  { background: #da3633; border-color: rgba(240,246,252,.1); color: #fff; }
  .btn-danger:hover { background: #b91c1c; }

  /* ── Row util ────────────────────────────────────────── */
  .row { display: flex; gap: 8px; align-items: flex-start; flex-wrap: wrap; }
  .row .gh-input { flex: 1 1 0; min-width: 0; }

  /* ── Result items ────────────────────────────────────── */
  .result-list { display: grid; gap: 8px; margin-top: 12px; }
  .result-item {
    background: var(--color-canvas-default);
    border: 1px solid var(--color-border-default);
    border-radius: var(--border-radius-small);
    padding: 12px;
    border-left: 3px solid var(--color-accent-emphasis);
  }
  .result-item .ref  { font-weight: 700; font-size: 14px; }
  .result-item .meta { color: var(--color-fg-muted); font-size: 12px; margin: 2px 0; }
  .result-item .snip { color: var(--color-fg-default); font-size: 13px; margin-top: 4px; }

  /* ── Ingest results ──────────────────────────────────── */
  .ingest-summary {
    background: var(--color-canvas-default);
    border: 1px solid var(--color-border-default);
    border-radius: var(--border-radius-small);
    padding: 12px;
    margin-top: 12px;
    font-size: 13px;
  }
  .ingest-summary .ok   { color: var(--color-success-fg); font-weight: 600; }
  .ingest-summary .dup  { color: var(--color-warning-fg); }
  .ingest-record {
    background: var(--color-canvas-subtle);
    border: 1px solid var(--color-border-muted);
    border-radius: 4px;
    padding: 8px 12px;
    margin-top: 6px;
    font-family: var(--font-mono);
    font-size: 12px;
  }

  /* ── Answer box ──────────────────────────────────────── */
  .answer-box {
    white-space: pre-wrap;
    line-height: 1.6;
    background: var(--color-canvas-default);
    border: 1px solid var(--color-border-default);
    border-radius: var(--border-radius-small);
    padding: 14px;
    font-size: 13px;
    margin-top: 10px;
  }
  .workspace-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
    margin-top: 10px;
  }
  .workspace-pane {
    background: var(--color-canvas-default);
    border: 1px solid var(--color-border-default);
    border-radius: var(--border-radius-small);
    padding: 10px;
    min-height: 180px;
  }
  .workspace-pane h4 {
    margin: 0 0 6px;
    font-size: 13px;
    color: var(--color-accent-fg);
  }
  .workspace-scroll {
    max-height: 320px;
    overflow: auto;
    white-space: pre-wrap;
    font-size: 12px;
    line-height: 1.45;
  }
  .workspace-list { margin: 0; padding-left: 18px; }
  .workspace-list li { margin: 4px 0; }
  .workspace-neg { color: var(--color-danger-fg); font-weight: 600; }
  .workspace-subtabs {
    display: flex;
    gap: 6px;
    margin-top: 10px;
    flex-wrap: wrap;
  }
  .workspace-subtab {
    background: var(--color-btn-bg);
    border: 1px solid var(--color-btn-border);
    color: var(--color-fg-muted);
    padding: 6px 12px;
    border-radius: var(--border-radius-small);
    font-size: 12px;
    cursor: pointer;
  }
  .workspace-subtab.active {
    color: var(--color-fg-default);
    border-color: var(--color-accent-fg);
    box-shadow: inset 0 -2px 0 var(--color-accent-fg);
  }
  .workspace-tab-pane {
    display: none;
    margin-top: 10px;
  }
  .workspace-tab-pane.active {
    display: block;
  }
  .workspace-panel {
    background: var(--color-canvas-default);
    border: 1px solid var(--color-border-default);
    border-radius: var(--border-radius-small);
    padding: 10px;
    min-height: 220px;
  }
  .workspace-panel h4 {
    margin: 0 0 6px;
    font-size: 13px;
    color: var(--color-accent-fg);
  }

  /* ── Misc ────────────────────────────────────────────── */
  .muted { color: var(--color-fg-muted); font-size: 12px; }
  .danger { color: var(--color-danger-fg); }
  .success { color: var(--color-success-fg); }
  a { color: var(--color-accent-fg); text-decoration: none; }
  a:hover { text-decoration: underline; }
  hr { border: none; border-top: 1px solid var(--color-border-muted); margin: 16px 0; }

  @media (max-width: 720px) {
    .stat-grid { grid-template-columns: repeat(2, 1fr); }
    .btn { padding: 5px 10px; font-size: 13px; }
    .workspace-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<!-- ── Top nav ─────────────────────────────────────────── -->
<header class="gh-header">
  <a class="logo" href="/"><img src="__LOGO_DATA_URI__" alt="Legal Sorter logo" /> <span>Legal Sorter</span></a>
  <nav>
    <a href="/docs" target="_blank" rel="noopener noreferrer">API Docs</a>
  </nav>
</header>

<!-- ── Page ────────────────────────────────────────────── -->
<main class="gh-page">

  <!-- Repo-style hero -->
  <div class="repo-hero">
    <div style="flex:1">
      <h1>📁 Legal Archive Console
        <span class="badge green">● live</span>
        <span class="badge blue">read + ingest</span>
      </h1>
      <p>Search indexed cases, inspect records, bulk-ingest raw legal text, and ask AI questions grounded in your archive.</p>
    </div>
  </div>

  <!-- Stats row -->
  <div id="stat-grid" class="stat-grid">
    <div class="stat-card"><div class="num">…</div><div class="lbl">Loading stats</div></div>
  </div>

  <!-- Tab bar -->
  <div class="gh-tabs">
    <button class="gh-tab active" onclick="switchTab('search',this)">🔍 Search <span class="count" id="tab-search-count"></span></button>
    <button class="gh-tab" onclick="switchTab('workspace',this)">🧩 Case Workspace</button>
    <button class="gh-tab" onclick="switchTab('ingest',this)">⬆ Bulk Ingest</button>
    <button class="gh-tab" onclick="switchTab('recent',this)">🕐 Recent Cases</button>
    <button class="gh-tab" onclick="switchTab('ai',this)">🤖 AI Assistant</button>
  </div>

  <!-- ── SEARCH panel ─────────────────────────────────── -->
  <div id="panel-search" class="panel active">
    <div class="gh-box">
      <div class="gh-box-header">Full-Text Search</div>
      <div class="gh-box-body">
        <div class="row">
          <input id="search-input" class="gh-input" type="text"
            placeholder='e.g. "qualified immunity" OR contract breach' />
          <button class="btn btn-primary" onclick="doSearch()">Search</button>
          <button class="btn" onclick="clearSearch()">Clear</button>
        </div>
        <div id="search-results" class="result-list"></div>
      </div>
    </div>
  </div>

  <!-- ── CASE WORKSPACE panel ──────────────────────────── -->
  <div id="panel-workspace" class="panel">
    <div class="gh-box">
      <div class="gh-box-header">Case Workspace</div>
      <div class="gh-box-body">
        <div class="row">
          <input id="workspace-doc-id" class="gh-input" type="text" placeholder="Paste document id…" />
          <button class="btn btn-primary" onclick="loadCaseWorkspace()">Open</button>
        </div>
        <div id="workspace-status" class="muted" style="margin-top:8px">Open a case from Search/Recent or enter a document id.</div>
        <div class="workspace-subtabs" id="workspace-subtabs">
          <button class="workspace-subtab active" data-tab="case" onclick="switchWorkspaceSubtab('case', this)">📄 Case Text</button>
          <button class="workspace-subtab" data-tab="citations" onclick="switchWorkspaceSubtab('citations', this)">📚 Citations</button>
          <button class="workspace-subtab" data-tab="rulings" onclick="switchWorkspaceSubtab('rulings', this)">⚖️ Rulings</button>
          <button class="workspace-subtab" data-tab="history" onclick="switchWorkspaceSubtab('history', this)">🧭 Overturned / History</button>
        </div>
        <div id="workspace-pane-case" class="workspace-tab-pane active">
          <div class="workspace-panel"><h4>📄 Case Text</h4><div id="workspace-case" class="workspace-scroll muted">No case loaded.</div></div>
        </div>
        <div id="workspace-pane-citations" class="workspace-tab-pane">
          <div class="workspace-panel"><h4>📚 Citations</h4><div id="workspace-citations" class="workspace-scroll muted">No case loaded.</div></div>
        </div>
        <div id="workspace-pane-rulings" class="workspace-tab-pane">
          <div class="workspace-panel"><h4>⚖️ Rulings</h4><div id="workspace-rulings" class="workspace-scroll muted">No case loaded.</div></div>
        </div>
        <div id="workspace-pane-history" class="workspace-tab-pane">
          <div class="workspace-panel"><h4>🧭 Overturned / History</h4><div id="workspace-history" class="workspace-scroll muted">No case loaded.</div></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ── BULK INGEST panel ─────────────────────────────── -->
  <div id="panel-ingest" class="panel">
    <div class="gh-box">
      <div class="gh-box-header">
        Bulk Legal Text Ingest
        <span class="muted">Paste one or many opinions — each separated by 3 blank lines</span>
      </div>
      <div class="gh-box-body">
        <p class="muted" style="margin:0 0 10px">
          Drop in any amount of raw legal text. The pipeline will
          <strong>split on 2 blank lines</strong>, content-hash each segment for
          <strong>de-duplication</strong>, run <strong>citation extraction</strong>,
          <strong>keyword tagging</strong>, and <strong>virtual-folder categorization</strong>
          before storing in the database.
        </p>
        <div style="margin-bottom:8px">
          <label class="muted" for="ingest-label">Optional label / case name</label>
          <input id="ingest-label" class="gh-input" type="text"
            placeholder="e.g. Smith v. Jones — SDNY 2023" style="margin-top:4px" />
        </div>
        <textarea id="ingest-text" class="gh-textarea" style="min-height:220px"
          placeholder="Paste raw legal text here — separate multiple opinions with 2 blank lines…"></textarea>
        <div class="row" style="margin-top:10px">
          <button id="ingest-btn" class="btn btn-primary" onclick="doIngest()">⬆ Ingest Text</button>
          <button class="btn" onclick="document.getElementById('ingest-text').value='';document.getElementById('ingest-results').innerHTML=''">Clear</button>
        </div>
        <div id="ingest-results"></div>
      </div>
    </div>
  </div>

  <!-- ── RECENT CASES panel ────────────────────────────── -->
  <div id="panel-recent" class="panel">
    <div class="gh-box">
      <div class="gh-box-header">Recent Cases</div>
      <div class="gh-box-body">
        <div class="row" style="margin-bottom:10px">
          <input id="folder-filter" class="gh-input" type="text" placeholder="Filter by folder prefix (optional)" />
          <button class="btn" onclick="loadRecentCases()">Refresh</button>
        </div>
        <div id="cases-results" class="result-list"></div>
      </div>
    </div>
  </div>

  <!-- ── AI ASSISTANT panel ────────────────────────────── -->
  <div id="panel-ai" class="panel">
    <div class="gh-box">
      <div class="gh-box-header">AI Assistant (RAG)</div>
      <div class="gh-box-body">
        <p class="muted" style="margin:0 0 10px">
          Answers are generated only from indexed case excerpts retrieved from your archive.
        </p>
        <textarea id="ask-input" class="gh-textarea"
          placeholder="Ask a legal question grounded in your indexed archive…"></textarea>
        <div class="row" style="margin-top:10px">
          <button id="ask-btn" class="btn btn-primary" onclick="doAsk()">Ask AI</button>
        </div>
        <div id="ask-results"></div>
        <div class="workspace-subtabs" id="ai-subtabs">
          <button class="workspace-subtab active" data-tab="answer" onclick="switchAiSubtab('answer', this)">🧠 LLM Answer</button>
          <button class="workspace-subtab" data-tab="sources" onclick="switchAiSubtab('sources', this)">🔗 Sources</button>
          <button class="workspace-subtab" data-tab="full" onclick="switchAiSubtab('full', this)">📖 Full Source Content</button>
        </div>
        <div id="ai-pane-answer" class="workspace-tab-pane active">
          <div class="workspace-panel"><h4>🧠 LLM Answer</h4><div id="ai-answer" class="workspace-scroll muted">No answer yet.</div></div>
        </div>
        <div id="ai-pane-sources" class="workspace-tab-pane">
          <div class="workspace-panel"><h4>🔗 Sources</h4><div id="ai-sources" class="workspace-scroll muted">No sources yet.</div></div>
        </div>
        <div id="ai-pane-full" class="workspace-tab-pane">
          <div class="workspace-panel"><h4>📖 Full Source Content</h4><div id="ai-source-full" class="workspace-scroll muted">Select a source after asking a question.</div></div>
        </div>
      </div>
    </div>
  </div>

</main>

<script>
/* ── Helpers ─────────────────────────────────────────── */
function escapeHtml(value) {
  const el = document.createElement('div');
  el.textContent = value ?? '';
  return el.innerHTML;
}
function safeSourceLink(url, label = 'Source ↗') {
  try {
    const p = new URL(url);
    if (!/^https?:$/i.test(p.protocol)) return '';
    return `<a href="${escapeHtml(p.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
  } catch (_) { return ''; }
}

/* ── Tab switching ───────────────────────────────────── */
function switchTab(name, btn) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.gh-tab').forEach(b => b.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'recent') loadRecentCases();
}

function switchWorkspaceSubtab(name, btn) {
  document.querySelectorAll('#workspace-subtabs .workspace-subtab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('[id^="workspace-pane-"]').forEach(p => p.classList.remove('active'));
  document.getElementById('workspace-pane-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
}

function switchAiSubtab(name, btn) {
  document.querySelectorAll('#ai-subtabs .workspace-subtab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('[id^="ai-pane-"]').forEach(p => p.classList.remove('active'));
  document.getElementById('ai-pane-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
}

/* ── Stats ───────────────────────────────────────────── */
async function loadStats() {
  const grid = document.getElementById('stat-grid');
  try {
    const d = await (await fetch('/api/stats')).json();
    const top = (d.top_folders || []).slice(0, 2);
    grid.innerHTML = `
      <div class="stat-card"><div class="num">${Number(d.total_cases||0).toLocaleString()}</div><div class="lbl">Indexed Cases</div></div>
      <div class="stat-card"><div class="num">${top[0]?top[0].count:0}</div><div class="lbl">Top Folder: ${escapeHtml(top[0]?.folder||'N/A')}</div></div>
      <div class="stat-card"><div class="num">${top[1]?top[1].count:0}</div><div class="lbl">2nd Folder: ${escapeHtml(top[1]?.folder||'N/A')}</div></div>
    `;
  } catch(_) {
    grid.innerHTML = '<div class="stat-card"><div class="lbl danger">Failed to load stats.</div></div>';
  }
}

/* ── Search ──────────────────────────────────────────── */
function resultCard(item) {
  const ref     = escapeHtml(item.ref_no || 'N/A');
  const folder  = escapeHtml(item.virtual_folder || 'Uncategorized');
  const snippet = escapeHtml(item.snippet || '');
  const src     = safeSourceLink(item.source_url) || '<span class="muted">No source URL</span>';
  const history = (item.subsequent_history || []).map(h =>
    `<li>${escapeHtml(h.ref_no || (h.doc_id || '').slice(0,12) || '?')} · ${escapeHtml(h.year || 'year?')} · ${escapeHtml(h.treatment || 'cited')}</li>`
  ).join('');
  return `
    <div class="result-item">
      <div class="ref">${ref}</div>
      <div class="meta">📁 ${folder}</div>
      <div class="snip">${snippet}</div>
      ${history ? `<div class="meta"><strong>Subsequent history</strong><ul style="margin:4px 0">${history}</ul></div>` : ''}
      <div class="meta">${src}</div>
      ${item.id ? `<div style="margin-top:8px"><button class="btn js-open-workspace" data-doc-id="${escapeHtml(item.id)}">Open workspace</button></div>` : ''}
    </div>`;
}
async function doSearch() {
  const q = document.getElementById('search-input').value.trim();
  const tgt = document.getElementById('search-results');
  if (!q) { tgt.innerHTML = '<div class="muted">Enter a query to search.</div>'; return; }
  tgt.innerHTML = '<div class="muted">Searching…</div>';
  try {
    const d = await (await fetch('/api/search?q=' + encodeURIComponent(q) + '&limit=20')).json();
    const res = d.results || [];
    const count = document.getElementById('tab-search-count');
    if (count) count.textContent = res.length || '';
    tgt.innerHTML = res.length ? res.map(resultCard).join('') : '<div class="muted">No matches found.</div>';
  } catch(_) { tgt.innerHTML = '<div class="danger">Search request failed.</div>'; }
}
function clearSearch() {
  document.getElementById('search-input').value = '';
  document.getElementById('search-results').innerHTML = '';
}

/* ── Bulk Ingest ─────────────────────────────────────── */
async function doIngest() {
  const text  = document.getElementById('ingest-text').value;
  const label = document.getElementById('ingest-label').value.trim();
  const btn   = document.getElementById('ingest-btn');
  const tgt   = document.getElementById('ingest-results');
  if (!text.trim()) { tgt.innerHTML = '<div class="muted">Paste some legal text first.</div>'; return; }

  btn.disabled = true;
  tgt.innerHTML = '<div class="muted">Processing… splitting, deduplicating, analyzing, storing…</div>';
  try {
    const r = await fetch('/api/ingest', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ text, label }),
    });
    const d = await r.json();
    if (!r.ok) { tgt.innerHTML = `<div class="danger">${escapeHtml(d.detail||'Ingest failed.')}</div>`; return; }

    const newRows = (d.new_records||[]).map(rec =>
      `<div class="ingest-record">✅ <strong>${escapeHtml(rec.ref_no||'?')}</strong> → ${escapeHtml(rec.virtual_folder||'Uncategorized')}</div>`
    ).join('');
    const dupRows = (d.duplicate_refs||[]).map(dupRef =>
      `<div class="ingest-record dup">⚠ duplicate: ${escapeHtml(dupRef)}</div>`
    ).join('');

    tgt.innerHTML = `
      <div class="ingest-summary">
        <div class="ok">✓ ${d.stored} new case(s) stored</div>
        ${d.duplicates_skipped ? `<div class="dup">⚠ ${d.duplicates_skipped} duplicate(s) skipped</div>` : ''}
        <div class="muted">${d.segments_submitted} segment(s) submitted</div>
        ${newRows}${dupRows}
      </div>`;
    loadStats(); // refresh counts
  } catch(_) {
    tgt.innerHTML = '<div class="danger">Ingest request failed.</div>';
  } finally {
    btn.disabled = false;
  }
}

/* ── Recent Cases ────────────────────────────────────── */
async function loadRecentCases() {
  const tgt = document.getElementById('cases-results');
  if (!tgt) return;
  tgt.innerHTML = '<div class="muted">Loading…</div>';
  try {
    const folder = document.getElementById('folder-filter').value.trim();
    const qs = new URLSearchParams({ limit: '20', offset: '0' });
    if (folder) qs.set('folder', folder);
    const d = await (await fetch('/api/cases?' + qs)).json();
    const rows = d.results || [];
    tgt.innerHTML = rows.length
      ? rows.map(c => `
          <div class="result-item">
            <div class="ref">${escapeHtml(c.ref_no||'N/A')}</div>
            <div class="meta">📁 ${escapeHtml(c.virtual_folder||'Uncategorized')}</div>
            <div class="meta">${escapeHtml(c.added_at||'')}</div>
            ${((c.subsequent_history||[]).length) ? `<div class="meta"><strong>Subsequent history</strong>: ${(c.subsequent_history||[]).map(h => `${escapeHtml(h.ref_no || (h.doc_id||'').slice(0,12) || '?')} · ${escapeHtml(h.treatment || 'cited')}`).join('; ')}</div>` : ''}
            ${c.id ? `<div style="margin-top:8px"><button class="btn js-open-workspace" data-doc-id="${escapeHtml(c.id)}">Open workspace</button></div>` : ''}
          </div>`).join('')
      : '<div class="muted">No cases found.</div>';
  } catch(_) { tgt.innerHTML = '<div class="danger">Failed to load case list.</div>'; }
}

function _rulingLogic(entities) {
  if (!entities || typeof entities !== 'object') return 'No explicit ruling logic extracted.';
  const raw = entities.RULING_LOGIC;
  if (!raw) return 'No explicit ruling logic extracted.';
  if (Array.isArray(raw)) return raw.join('\n');
  return String(raw);
}

async function loadCaseWorkspace(docIdValue = null) {
  const idInput = document.getElementById('workspace-doc-id');
  const docId = (docIdValue || (idInput ? idInput.value.trim() : '')).trim();
  const status = document.getElementById('workspace-status');
  const casePane = document.getElementById('workspace-case');
  const citationsPane = document.getElementById('workspace-citations');
  const rulingsPane = document.getElementById('workspace-rulings');
  const historyPane = document.getElementById('workspace-history');
  if (!docId) {
    status.textContent = 'Enter a document id first.';
    return;
  }
  if (idInput) idInput.value = docId;
  status.textContent = 'Loading workspace…';
  try {
    const d = await (await fetch('/api/case/' + encodeURIComponent(docId))).json();
    if (d.detail) throw new Error(String(d.detail));
    casePane.textContent = (d.full_text || d.preview || '').trim() || 'No text available.';
    const citations = Array.isArray(d.citations) ? d.citations : [];
    citationsPane.innerHTML = citations.length
      ? `<ul class="workspace-list">${citations.map(c => `<li>${escapeHtml(String(c))}</li>`).join('')}</ul>`
      : '<span class="muted">No citations found.</span>';
    rulingsPane.textContent = _rulingLogic(d.entities);
    const history = Array.isArray(d.subsequent_history) ? d.subsequent_history : [];
    historyPane.innerHTML = history.length
      ? `<ul class="workspace-list">${history.map(h => {
          const treatment = String(h.treatment || 'cited');
          const cls = ['overruled','criticized','limited'].includes(treatment.toLowerCase()) ? 'workspace-neg' : '';
          const label = escapeHtml(h.ref_no || (h.doc_id || '').slice(0,12) || '?');
          const yr = escapeHtml(String(h.year || 'year?'));
          const ctx = escapeHtml(String(h.context || ''));
          return `<li><span class="${cls}">${label} · ${yr} · ${escapeHtml(treatment)}</span>${ctx ? `<br/><span class="muted">${ctx}</span>` : ''}</li>`;
        }).join('')}</ul>`
      : '<span class="muted">No subsequent history recorded.</span>';
    status.textContent = `Loaded workspace for ${d.ref_no || docId}.`;
  } catch (err) {
    status.textContent = 'Failed to load workspace.';
    casePane.textContent = '';
    citationsPane.innerHTML = '';
    rulingsPane.textContent = '';
    historyPane.innerHTML = `<span class="danger">${escapeHtml(String(err))}</span>`;
  }
}

function openCaseWorkspace(encodedDocId) {
  const docId = String(encodedDocId || '').trim();
  const tabBtn = Array.from(document.querySelectorAll('.gh-tab')).find(b => b.textContent.includes('Case Workspace'));
  if (tabBtn) switchTab('workspace', tabBtn);
  const firstWorkspaceSubtab = document.querySelector('#workspace-subtabs .workspace-subtab');
  switchWorkspaceSubtab('case', firstWorkspaceSubtab);
  loadCaseWorkspace(docId);
}

/* ── AI Ask ──────────────────────────────────────────── */
async function doAsk() {
  const q   = document.getElementById('ask-input').value.trim();
  const btn = document.getElementById('ask-btn');
  const tgt = document.getElementById('ask-results');
  if (!q) { tgt.innerHTML = '<div class="muted">Enter a question first.</div>'; return; }
  btn.disabled = true;
  tgt.innerHTML = '<div class="muted">Generating grounded answer…</div>';
  try {
    const r = await fetch('/api/ask', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({question:q, top_k:5}),
    });
    const d = await r.json();
    if (!r.ok) { tgt.innerHTML = `<div class="danger">${escapeHtml(d.detail||'LLM request failed.')}</div>`; return; }
    const answer  = escapeHtml(d.answer||'No answer returned.');
    const answerPane = document.getElementById('ai-answer');
    const sourcesPane = document.getElementById('ai-sources');
    const fullPane = document.getElementById('ai-source-full');
    answerPane.innerHTML = `<div class="answer-box">${answer}</div>`;
    const sources = (d.sources||[]).map((s, idx) => {
      const lbl  = `${escapeHtml(s.ref_no||(s.doc_id||'').slice(0,12)||'?')} — ${escapeHtml(s.virtual_folder||'Uncategorized')}`;
      const link = s.source_url ? ` ${safeSourceLink(s.source_url,'↗')}` : '';
      return `<li><button class="btn" onclick="showAiSource(${idx})">Open</button> ${lbl}${link}</li>`;
    }).join('');
    window.__aiSources = Array.isArray(d.sources) ? d.sources : [];
    sourcesPane.innerHTML = sources ? `<ul class="workspace-list">${sources}</ul>` : '<span class="muted">No sources returned.</span>';
    if (window.__aiSources.length) {
      const sourcesTab = document.querySelector('#ai-subtabs .workspace-subtab[data-tab="sources"]');
      switchAiSubtab('sources', sourcesTab);
      showAiSource(0);
    } else {
      fullPane.textContent = 'No source content available.';
    }
    tgt.innerHTML = `<div class="muted">Done. ${window.__aiSources.length} source(s) returned.</div>`;
  } catch(_) {
    tgt.innerHTML = '<div class="danger">AI request failed.</div>';
  } finally { btn.disabled = false; }
}

async function showAiSource(index) {
  const sources = Array.isArray(window.__aiSources) ? window.__aiSources : [];
  const s = sources[index];
  const fullPane = document.getElementById('ai-source-full');
  if (!s) {
    fullPane.textContent = 'Source not found.';
    return;
  }
  const label = `${s.ref_no || (s.doc_id || '').slice(0,12) || '?'} — ${s.virtual_folder || 'Uncategorized'}`;
  let text = (s.source_text || s.source_preview || '').trim();
  if (!text && s.doc_id) {
    try {
      const d = await (await fetch('/api/case/' + encodeURIComponent(s.doc_id))).json();
      text = (d.full_text || d.preview || '').trim();
    } catch(_) {}
  }
  fullPane.innerHTML = `<strong>${escapeHtml(label)}</strong>\n\n${escapeHtml(text || 'No source text available.')}`;
}

/* ── Boot ────────────────────────────────────────────── */
document.getElementById('search-input').addEventListener('keydown', e => { if (e.key==='Enter') doSearch(); });
document.getElementById('folder-filter') && document.getElementById('folder-filter').addEventListener('keydown', e => { if (e.key==='Enter') loadRecentCases(); });
document.getElementById('workspace-doc-id') && document.getElementById('workspace-doc-id').addEventListener('keydown', e => { if (e.key==='Enter') loadCaseWorkspace(); });
document.addEventListener('click', (event) => {
  const btn = event.target.closest('.js-open-workspace');
  if (!btn) return;
  const docId = btn.getAttribute('data-doc-id') || '';
  openCaseWorkspace(docId);
});
loadStats();
</script>
</body>
</html>
"""

_HTML = _HTML_TEMPLATE.replace("__LOGO_DATA_URI__", _LOGO_DATA_URI)

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    """Serve the single-page browser UI."""
    return HTMLResponse(content=_HTML)
