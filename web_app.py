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

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Legal Sorter — Archive Console</title>
<style>
  :root {
    --bg: #0f1220;
    --panel: #171b2e;
    --panel-2: #1e2440;
    --text: #e8ecff;
    --muted: #a8b0d9;
    --accent: #7f8cff;
    --accent-2: #35d4a6;
    --border: #2b345e;
    --danger: #ff6b7a;
    --shadow: 0 10px 30px rgba(0,0,0,.25);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: Inter, Segoe UI, system-ui, sans-serif;
    background: radial-gradient(circle at top, #1a2142, var(--bg) 40%);
    color: var(--text);
  }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 20px; }
  .hero {
    background: linear-gradient(120deg, #1d2750, #11162b);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 20px;
    box-shadow: var(--shadow);
    display: flex;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }
  .hero h1 { margin: 0 0 6px; font-size: 1.5rem; }
  .hero p { margin: 0; color: var(--muted); }
  .pill {
    display: inline-block;
    font-size: .75rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 4px 10px;
    color: #d8ddff;
    background: #171e3c;
  }
  .grid { margin-top: 16px; display: grid; gap: 14px; grid-template-columns: repeat(12, minmax(0, 1fr)); }
  .card {
    background: linear-gradient(180deg, var(--panel), #13182d);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 14px;
    box-shadow: var(--shadow);
  }
  .span-12 { grid-column: span 12; }
  .span-8 { grid-column: span 8; }
  .span-4 { grid-column: span 4; }
  .stats { display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: 10px; }
  .stat { background: var(--panel-2); border: 1px solid var(--border); border-radius: 10px; padding: 12px; }
  .stat .num { font-size: 1.4rem; font-weight: 700; color: var(--accent-2); }
  .stat .label { color: var(--muted); font-size: .82rem; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; }
  input, textarea, button {
    font: inherit;
    border-radius: 10px;
    border: 1px solid var(--border);
  }
  input, textarea {
    width: 100%;
    background: #0f1530;
    color: var(--text);
    padding: 10px 12px;
    outline: none;
  }
  textarea { min-height: 92px; resize: vertical; }
  input:focus, textarea:focus { border-color: var(--accent); }
  button {
    background: linear-gradient(180deg, #8b96ff, #6776ff);
    color: #fff;
    border: none;
    padding: 10px 14px;
    cursor: pointer;
    font-weight: 600;
  }
  button.secondary { background: #2a3158; color: #d8defe; }
  button:disabled { opacity: .6; cursor: default; }
  .results { margin-top: 10px; display: grid; gap: 8px; }
  .result {
    background: #101735;
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: 10px;
    padding: 10px;
  }
  .meta { color: var(--muted); font-size: .82rem; margin: 3px 0; }
  .snippet { color: #d7ddff; font-size: .92rem; }
  .answer { white-space: pre-wrap; line-height: 1.5; background: #101735; border: 1px solid var(--border); border-radius: 10px; padding: 10px; }
  .error { color: var(--danger); }
  .small { color: var(--muted); font-size: .82rem; }
  a { color: #a5b2ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  @media (max-width: 980px) {
    .span-8, .span-4 { grid-column: span 12; }
    .stats { grid-template-columns: repeat(2, minmax(0,1fr)); }
  }
</style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div>
        <span class="pill">READ-ONLY ARCHIVE</span>
        <h1>⚖️ Legal Sorter — Archive Console</h1>
        <p>Search indexed cases, inspect records, and ask grounded AI questions from your local archive.</p>
      </div>
      <div class="small">
        API Docs: <a href="/docs" target="_blank" rel="noopener noreferrer">/docs</a>
      </div>
    </section>

    <div class="grid">
      <section class="card span-12">
        <h3>Archive Health</h3>
        <div id="stats" class="stats"><span class="small">Loading...</span></div>
      </section>

      <section class="card span-8">
        <h3>Search Cases</h3>
        <div class="row">
          <input id="search-input" type="text" placeholder='Try: "qualified immunity" OR contract liability' />
          <button onclick="doSearch()">Search</button>
          <button class="secondary" onclick="clearSearch()">Clear</button>
        </div>
        <div id="search-results" class="results"></div>
      </section>

      <section class="card span-4">
        <h3>Recent Cases</h3>
        <div class="row" style="margin-bottom:8px;">
          <input id="folder-filter" type="text" placeholder="Folder prefix (optional)" />
          <button class="secondary" onclick="loadRecentCases()">Refresh</button>
        </div>
        <div id="cases-results" class="results"></div>
      </section>

      <section class="card span-12">
        <h3>AI Assistant (RAG)</h3>
        <p class="small">Answers are generated only from indexed case excerpts returned by retrieval.</p>
        <textarea id="ask-input" placeholder="Ask a legal question grounded in your indexed archive"></textarea>
        <div class="row" style="margin-top:8px;">
          <button id="ask-btn" onclick="doAsk()">Ask AI</button>
        </div>
        <div id="ask-results" class="results"></div>
      </section>
    </div>
  </div>

<script>
function escapeHtml(value) {
  const el = document.createElement('div');
  el.textContent = value ?? '';
  return el.innerHTML;
}

async function loadStats() {
  const target = document.getElementById('stats');
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    const top = (d.top_folders || []).slice(0, 2);
    target.innerHTML = `
      <div class="stat"><div class="num">${Number(d.total_cases || 0).toLocaleString()}</div><div class="label">Indexed Cases</div></div>
      <div class="stat"><div class="num">${top[0] ? top[0].count : 0}</div><div class="label">Top Folder 1: ${escapeHtml(top[0]?.folder || 'N/A')}</div></div>
      <div class="stat"><div class="num">${top[1] ? top[1].count : 0}</div><div class="label">Top Folder 2: ${escapeHtml(top[1]?.folder || 'N/A')}</div></div>
    `;
  } catch (e) {
    target.innerHTML = '<div class="error">Failed to load archive stats.</div>';
  }
}

function resultCard(item) {
  const ref = escapeHtml(item.ref_no || 'N/A');
  const folder = escapeHtml(item.virtual_folder || 'Uncategorized');
  const snippet = escapeHtml(item.snippet || '');
  const source = item.source_url && /^https?:\/\//i.test(item.source_url)
    ? `<a href="${encodeURI(item.source_url)}" target="_blank" rel="noopener noreferrer">Source ↗</a>`
    : '<span class="small">No source URL</span>';
  return `
    <div class="result">
      <div><strong>${ref}</strong></div>
      <div class="meta">📁 ${folder}</div>
      <div class="snippet">${snippet}</div>
      <div class="meta">${source}</div>
    </div>
  `;
}

async function doSearch() {
  const q = document.getElementById('search-input').value.trim();
  const target = document.getElementById('search-results');
  if (!q) {
    target.innerHTML = '<div class="small">Enter a query to search.</div>';
    return;
  }

  target.innerHTML = '<div class="small">Searching...</div>';
  try {
    const r = await fetch('/api/search?q=' + encodeURIComponent(q) + '&limit=20');
    const d = await r.json();
    const results = d.results || [];
    if (!results.length) {
      target.innerHTML = '<div class="small">No matches found.</div>';
      return;
    }
    target.innerHTML = results.map(resultCard).join('');
  } catch (e) {
    target.innerHTML = '<div class="error">Search request failed.</div>';
  }
}

function clearSearch() {
  document.getElementById('search-input').value = '';
  document.getElementById('search-results').innerHTML = '';
}

async function loadRecentCases() {
  const target = document.getElementById('cases-results');
  target.innerHTML = '<div class="small">Loading...</div>';
  try {
    const folder = document.getElementById('folder-filter').value.trim();
    const qs = new URLSearchParams({ limit: '10', offset: '0' });
    if (folder) qs.set('folder', folder);
    const r = await fetch('/api/cases?' + qs.toString());
    const d = await r.json();
    const rows = d.results || [];
    if (!rows.length) {
      target.innerHTML = '<div class="small">No cases found.</div>';
      return;
    }
    target.innerHTML = rows.map(c => `
      <div class="result">
        <div><strong>${escapeHtml(c.ref_no || 'N/A')}</strong></div>
        <div class="meta">📁 ${escapeHtml(c.virtual_folder || 'Uncategorized')}</div>
        <div class="meta">${escapeHtml(c.added_at || '')}</div>
      </div>
    `).join('');
  } catch (e) {
    target.innerHTML = '<div class="error">Failed to load case list.</div>';
  }
}

async function doAsk() {
  const q = document.getElementById('ask-input').value.trim();
  const btn = document.getElementById('ask-btn');
  const target = document.getElementById('ask-results');
  if (!q) {
    target.innerHTML = '<div class="small">Enter a question first.</div>';
    return;
  }

  btn.disabled = true;
  target.innerHTML = '<div class="small">Generating grounded answer...</div>';
  try {
    const r = await fetch('/api/ask', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ question: q, top_k: 5 }),
    });
    const d = await r.json();
    if (!r.ok) {
      target.innerHTML = `<div class="error">${escapeHtml(d.detail || 'LLM request failed.')}</div>`;
      return;
    }
    const answer = escapeHtml(d.answer || 'No answer returned.');
    const sources = (d.sources || []).map(s => {
      const label = `${escapeHtml(s.ref_no || (s.doc_id || '').slice(0, 12) || '?')} — ${escapeHtml(s.virtual_folder || 'Uncategorized')}`;
      const link = (s.source_url && /^https?:\/\//i.test(s.source_url))
        ? ` <a href="${encodeURI(s.source_url)}" target="_blank" rel="noopener noreferrer">↗</a>`
        : '';
      return `<li>${label}${link}</li>`;
    }).join('');

    target.innerHTML = `
      <div class="answer">${answer}</div>
      ${sources ? `<div class="meta"><strong>Sources</strong><ul>${sources}</ul></div>` : ''}
    `;
  } catch (e) {
    target.innerHTML = '<div class="error">AI request failed.</div>';
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('search-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') doSearch();
});

document.getElementById('folder-filter').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') loadRecentCases();
});

loadStats();
loadRecentCases();
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    """Serve the single-page browser UI."""
    return HTMLResponse(content=_HTML)
