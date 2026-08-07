"""Read-only web interface for the Legal Sorter archive.

Exposes the indexed legal case database as a searchable website.
All endpoints are read-only — no data is written or modified.

Run:
    uvicorn web_app:app --host 0.0.0.0 --port 8000

Then open http://localhost:8000 in your browser.

Configuration:
    The app reads config.yaml for the database path.
    Set LLM_API_KEY in your environment to enable AI-powered Q&A.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from src import config as cfgmod

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Legal Sorter — Case Archive",
    description="Read-only search and AI Q&A interface for indexed legal cases.",
    version="1.0.0",
)


def _db_path() -> str:
    cfg = cfgmod.load_config()
    return str(Path(cfg["index_folder"]) / "legal_sorter.db")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str
    top_k: int = 5


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/api/search", tags=["Search"])
def search(
    q: str = Query(..., description="Search query — supports FTS5 phrases and AND/OR"),
    limit: int = Query(default=20, le=100),
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
        return {"query": q, "count": len(results), "results": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/case/{doc_id}", tags=["Cases"])
def get_case(doc_id: str):
    """Retrieve full metadata and text for a single indexed case."""
    try:
        with sqlite3.connect(_db_path()) as conn:
            row = conn.execute(
                """SELECT id, ref_no, virtual_folder, source_url, file_type,
                          entities_json, citations_json, keywords_json,
                          SUBSTR(text, 1, 8000) as preview, added_at
                   FROM documents WHERE id = ?""",
                (doc_id,),
            ).fetchone()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not row:
        raise HTTPException(status_code=404, detail=f"Case {doc_id[:12]} not found.")

    def _j(v):
        try:
            return json.loads(v) if v else []
        except Exception:
            return []

    return {
        "id": row[0],
        "ref_no": row[1],
        "virtual_folder": row[2],
        "source_url": row[3],
        "file_type": row[4],
        "entities": _j(row[5]),
        "citations": _j(row[6]),
        "keywords": _j(row[7]),
        "preview": (row[8] or "").replace("\n", " ").strip(),
        "added_at": row[9],
    }


@app.get("/api/cases", tags=["Cases"])
def list_cases(
    folder: Optional[str] = Query(default=None, description="Filter by virtual_folder prefix"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List indexed cases, optionally filtered by virtual folder."""
    try:
        with sqlite3.connect(_db_path()) as conn:
            if folder:
                rows = conn.execute(
                    """SELECT id, ref_no, virtual_folder, source_url, added_at
                       FROM documents WHERE virtual_folder LIKE ?
                       ORDER BY added_at DESC LIMIT ? OFFSET ?""",
                    (f"{folder}%", limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, ref_no, virtual_folder, source_url, added_at
                       FROM documents ORDER BY added_at DESC LIMIT ? OFFSET ?""",
                    (limit, offset),
                ).fetchall()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "count": len(rows),
        "offset": offset,
        "results": [
            {
                "id": r[0],
                "ref_no": r[1],
                "virtual_folder": r[2],
                "source_url": r[3],
                "added_at": r[4],
            }
            for r in rows
        ],
    }


@app.post("/api/ask", tags=["AI"])
def ask(body: AskRequest):
    """Ask a legal question. The AI answers using only the indexed cases as context.

    Requires LLM_API_KEY environment variable (or a local Ollama endpoint in config.yaml).
    """
    try:
        from src.legal_ai import query_cases
        answer, sources = query_cases(_db_path(), body.question, top_k=body.top_k)
        return {"answer": answer, "sources": sources}
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"LLM error: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/stats", tags=["Info"])
def stats():
    """Return basic statistics about the indexed archive."""
    try:
        with sqlite3.connect(_db_path()) as conn:
            total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            folders = conn.execute(
                "SELECT virtual_folder, COUNT(*) FROM documents GROUP BY virtual_folder ORDER BY 2 DESC LIMIT 20"
            ).fetchall()
            latest = conn.execute(
                "SELECT ref_no, virtual_folder, added_at FROM documents ORDER BY added_at DESC LIMIT 5"
            ).fetchall()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "total_cases": total,
        "top_folders": [{"folder": r[0], "count": r[1]} for r in folders],
        "recently_added": [{"ref_no": r[0], "folder": r[1], "added_at": r[2]} for r in latest],
    }


# ---------------------------------------------------------------------------
# Browser UI (single-page HTML)
# ---------------------------------------------------------------------------

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Legal Sorter — Case Archive</title>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; margin: 0; background: #f5f6fa; color: #1a1a2e; }
  header { background: #1a1a2e; color: #fff; padding: 1rem 2rem; display: flex; align-items: center; gap: 1rem; }
  header h1 { margin: 0; font-size: 1.4rem; }
  .badge { background: #e94560; color: #fff; font-size: .7rem; padding: 2px 6px; border-radius: 4px; }
  main { max-width: 960px; margin: 2rem auto; padding: 0 1rem; }
  .card { background: #fff; border-radius: 8px; padding: 1.5rem; margin-bottom: 1rem; box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  .card h2 { margin: 0 0 1rem; font-size: 1.1rem; }
  .row { display: flex; gap: .5rem; }
  input[type=text], textarea {
    flex: 1; padding: .6rem .8rem; border: 1px solid #ddd; border-radius: 6px;
    font-size: .95rem; font-family: inherit;
  }
  textarea { resize: vertical; min-height: 72px; }
  button {
    padding: .6rem 1.2rem; background: #e94560; color: #fff; border: none;
    border-radius: 6px; cursor: pointer; font-size: .95rem; white-space: nowrap;
  }
  button:hover { background: #c73652; }
  button:disabled { background: #aaa; cursor: default; }
  .results { margin-top: 1rem; }
  .result-item {
    border-left: 3px solid #e94560; padding: .6rem .8rem; margin-bottom: .5rem;
    background: #fafafa; border-radius: 0 6px 6px 0;
  }
  .result-item .ref { font-weight: bold; color: #e94560; }
  .result-item .folder { font-size: .8rem; color: #666; margin: 2px 0; }
  .result-item .snippet { font-size: .85rem; color: #444; margin-top: 4px; }
  .answer-box {
    background: #f0f4ff; border: 1px solid #c5d0f0; border-radius: 8px;
    padding: 1rem; white-space: pre-wrap; font-size: .9rem; line-height: 1.6;
  }
  .sources { margin-top: .5rem; font-size: .8rem; color: #555; }
  .stat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: .5rem; }
  .stat-item { background: #f0f4ff; border-radius: 6px; padding: .8rem; text-align: center; }
  .stat-item .num { font-size: 1.8rem; font-weight: bold; color: #e94560; }
  .stat-item .label { font-size: .8rem; color: #555; }
  .error { color: #c0392b; background: #ffeaea; padding: .5rem .8rem; border-radius: 6px; }
  .loading { color: #888; font-style: italic; }
</style>
</head>
<body>
<header>
  <h1>⚖️ Legal Sorter</h1>
  <span class="badge">READ-ONLY ARCHIVE</span>
</header>
<main>

<!-- Stats banner -->
<div class="card" id="stats-card">
  <h2>Archive Overview</h2>
  <div class="stat-grid" id="stats-grid"><span class="loading">Loading…</span></div>
</div>

<!-- Search -->
<div class="card">
  <h2>🔍 Full-Text Case Search</h2>
  <div class="row">
    <input type="text" id="search-input" placeholder='e.g.  qualified immunity  OR  "breach of contract"' />
    <button onclick="doSearch()">Search</button>
  </div>
  <div class="results" id="search-results"></div>
</div>

<!-- AI Q&A -->
<div class="card">
  <h2>🤖 Ask the AI (RAG — grounded in indexed cases only)</h2>
  <textarea id="ask-input" placeholder="e.g. What is the standard for qualified immunity under the Ninth Circuit?"></textarea>
  <div class="row" style="margin-top:.5rem">
    <button id="ask-btn" onclick="doAsk()">Ask</button>
  </div>
  <div class="results" id="ask-results"></div>
</div>

</main>

<script>
async function loadStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    const grid = document.getElementById('stats-grid');
    grid.innerHTML = `
      <div class="stat-item"><div class="num">${d.total_cases.toLocaleString()}</div><div class="label">Indexed Cases</div></div>
      ${d.top_folders.slice(0,5).map(f =>
        `<div class="stat-item"><div class="num">${f.count}</div><div class="label">${f.folder||'Unsorted'}</div></div>`
      ).join('')}
    `;
  } catch(e) {
    document.getElementById('stats-grid').innerHTML = '<span class="error">Could not load stats.</span>';
  }
}

async function doSearch() {
  const q = document.getElementById('search-input').value.trim();
  if (!q) return;
  const el = document.getElementById('search-results');
  el.innerHTML = '<span class="loading">Searching\u2026</span>';
  try {
    const r = await fetch('/api/search?q=' + encodeURIComponent(q) + '&limit=20');
    const d = await r.json();
    if (!d.results.length) { el.innerHTML = '<p>No results found.</p>'; return; }
    el.innerHTML = d.results.map(c => {
      const refEl = document.createElement('span');
      refEl.className = 'ref';
      refEl.textContent = c.ref_no || 'N/A';

      const folderEl = document.createElement('div');
      folderEl.className = 'folder';
      folderEl.textContent = '\uD83D\uDCC1 ' + (c.virtual_folder || 'Uncategorized');

      const snippetEl = document.createElement('div');
      snippetEl.className = 'snippet';
      snippetEl.textContent = c.snippet || '';

      let anchorHtml = '';
      if (c.source_url && /^https?:[/][/]/i.test(c.source_url)) {
        const safeUrl = encodeURI(c.source_url);
        anchorHtml = `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer" style="font-size:.8rem">View Source \u2197</a>`;
      }

      return (
        '<div class="result-item">' +
        refEl.outerHTML +
        folderEl.outerHTML +
        snippetEl.outerHTML +
        anchorHtml +
        '</div>'
      );
    }).join('');
  } catch(e) {
    el.innerHTML = `<div class="error">Search error: ${e}</div>`;
  }
}

async function doAsk() {
  const q = document.getElementById('ask-input').value.trim();
  if (!q) return;
  const btn = document.getElementById('ask-btn');
  const el = document.getElementById('ask-results');
  btn.disabled = true;
  el.innerHTML = '<span class="loading">Consulting the archive\u2026</span>';
  try {
    const r = await fetch('/api/ask', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({question: q, top_k: 5})
    });
    const d = await r.json();
    if (r.ok) {
      // Render answer as plain text to prevent XSS from LLM output
      const answerBox = document.createElement('div');
      answerBox.className = 'answer-box';
      answerBox.textContent = d.answer || '';

      const sources = (d.sources||[]).map(s => {
        const label = (s.ref_no || (s.doc_id || '').slice(0,12) || '?') +
                      (s.virtual_folder ? ' \u2014 ' + s.virtual_folder : '');
        let linkHtml = '';
        if (s.source_url && /^https?:[/][/]/i.test(s.source_url)) {
          const safeUrl = encodeURI(s.source_url);
          linkHtml = ` <a href="${safeUrl}" target="_blank" rel="noopener noreferrer">\u2197</a>`;
        }
        return label + linkHtml;
      }).join('<br>');

      el.innerHTML = '';
      el.appendChild(answerBox);
      if (sources) {
        const srcDiv = document.createElement('div');
        srcDiv.className = 'sources';
        srcDiv.innerHTML = '<b>Sources:</b><br>' + sources;
        el.appendChild(srcDiv);
      }
    } else {
      const errBox = document.createElement('div');
      errBox.className = 'error';
      errBox.textContent = d.detail || 'Unknown error';
      el.innerHTML = '';
      el.appendChild(errBox);
    }
  } catch(e) {
    el.innerHTML = `<div class="error">Request failed: ${e}</div>`;
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('search-input').addEventListener('keydown', e => { if (e.key==='Enter') doSearch(); });
loadStats();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    """Serve the single-page browser UI."""
    return HTMLResponse(content=_HTML)
