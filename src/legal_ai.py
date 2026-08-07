"""LLM-powered AI assistant for the Legal Sorter archive.

Provides three practical entry-points:

    query_cases(db_path, question, top_k=5)
        Keyword-retrieves the most relevant cases, then asks the LLM to
        synthesise an answer grounded in those documents.  Returns a
        (answer_text, sources) tuple where sources is a list of dicts
        with keys: doc_id, ref_no, virtual_folder, source_url.

    analyze_case(db_path, doc_id, instruction)
        Pulls the full text of one case and sends it to the LLM with a
        custom instruction (summarise, find weaknesses, compare, …).
        Returns the LLM response string.

    semantic_search(db_path, query, top_k=8)
        Returns the top-k cases keyword-ranked, each decorated with a
        short LLM-written relevance sentence.
        Returns a list of dicts with keys: doc_id, ref_no, virtual_folder,
        source_url, snippet, relevance_note.

Configuration is read from config.yaml (llm section).
The API key is always read from the LLM_API_KEY environment variable –
never from the config file.
"""

from __future__ import annotations

import os
import re
import json
import sqlite3
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_llm_cfg() -> dict:
    """Return the [llm] section of config.yaml, with safe defaults."""
    try:
        from . import config as cfgmod  # relative import when used as a package
        cfg = cfgmod.load_config()
    except ImportError:
        try:
            import config as cfgmod  # direct run / test context
            cfg = cfgmod.load_config()
        except Exception:
            cfg = {}
    llm = cfg.get("llm", {})
    return {
        "base_url": llm.get("base_url", "https://api.openai.com/v1").rstrip("/"),
        "api_key": os.environ.get("LLM_API_KEY", ""),
        "model": llm.get("model", "gpt-4o-mini"),
        "max_context_chars": int(llm.get("max_context_chars", 12000)),
    }


# ---------------------------------------------------------------------------
# Low-level HTTP call (avoids adding openai SDK as a hard dep)
# ---------------------------------------------------------------------------

def _chat(messages: list[dict], cfg: dict) -> str:
    """POST to any OpenAI-compatible /chat/completions endpoint."""
    import urllib.request

    payload = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.2,
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
    }

    url = f"{cfg['base_url']}/chat/completions"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        log.error("LLM request failed: %s", exc)
        raise RuntimeError(f"LLM request failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

@dataclass
class _CaseRow:
    doc_id: str
    ref_no: str | None
    virtual_folder: str | None
    source_url: str | None
    text: str
    keywords: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)


_MAX_LOAD = 2000  # max rows scanned for keyword ranking


def _load_all(db_path: str) -> list[_CaseRow]:
    rows: list[_CaseRow] = []
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                """SELECT id, ref_no, virtual_folder, source_url, text,
                          keywords_json, citations_json
                   FROM documents
                   WHERE text IS NOT NULL AND text != ''
                   ORDER BY added_at DESC
                   LIMIT ?""",
                (_MAX_LOAD,),
            )
            for r in cur.fetchall():
                rows.append(_CaseRow(
                    doc_id=r[0],
                    ref_no=r[1],
                    virtual_folder=r[2],
                    source_url=r[3],
                    text=r[4] or "",
                    keywords=_safe_json(r[5], []),
                    citations=_safe_json(r[6], []),
                ))
    except Exception as exc:
        log.warning("DB load failed: %s", exc)
    return rows


def _load_one(db_path: str, doc_id: str) -> _CaseRow | None:
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                """SELECT id, ref_no, virtual_folder, source_url, text,
                          keywords_json, citations_json
                   FROM documents WHERE id = ?""",
                (doc_id,),
            )
            r = cur.fetchone()
            if not r:
                return None
            return _CaseRow(
                doc_id=r[0], ref_no=r[1], virtual_folder=r[2],
                source_url=r[3], text=r[4] or "",
                keywords=_safe_json(r[5], []),
                citations=_safe_json(r[6], []),
            )
    except Exception as exc:
        log.warning("DB load_one failed: %s", exc)
        return None


def _safe_json(raw: Any, fallback: Any) -> Any:
    try:
        return json.loads(raw) if raw else fallback
    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# Keyword ranking (fast, no ML dependency)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z]{3,}", (text or "").lower())


def _score(row: _CaseRow, query_terms: set[str]) -> float:
    haystack = " ".join([
        row.virtual_folder or "",
        " ".join(row.keywords),
        " ".join(row.citations),
        row.text[:8000],
    ]).lower()
    score = 0.0
    for term in query_terms:
        if term in haystack:
            score += 1.0
            score += min(haystack.count(term), 5) * 0.15
    return score


def _rank(rows: list[_CaseRow], query: str, top_k: int) -> list[_CaseRow]:
    terms = set(_tokenize(query))
    if not terms:
        return rows[:top_k]
    scored = [(r, _score(r, terms)) for r in rows]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [r for r, s in scored if s > 0][:top_k]


def _snippet(text: str, query_terms: set[str], max_chars: int = 400) -> str:
    """Pick the sentence in text that has the most query-term hits."""
    sentences = re.split(r"(?<=[.!?])\s+", text[:10000])
    best, best_score = "", -1
    for s in sentences:
        s_clean = s.strip().replace("\n", " ")
        if len(s_clean) < 40:
            continue
        sc = sum(1 for t in query_terms if t in s_clean.lower())
        if sc > best_score:
            best, best_score = s_clean, sc
    return (best or text[:max_chars].replace("\n", " "))[:max_chars]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def query_cases(
    db_path: str,
    question: str,
    top_k: int = 5,
) -> tuple[str, list[dict]]:
    """Ask a legal question; answer is grounded in the top-k indexed cases.

    Returns:
        (answer_str, sources_list)
        sources_list entries: {doc_id, ref_no, virtual_folder, source_url}
    """
    cfg = _load_llm_cfg()
    rows = _load_all(db_path)
    if not rows:
        return ("No indexed documents found. Ingest cases first.", [])

    top = _rank(rows, question, top_k)
    if not top:
        return ("No relevant cases found for that query. Try different search terms.", [])

    max_chars = cfg["max_context_chars"]
    context_blocks: list[str] = []
    for i, r in enumerate(top, 1):
        snippet = r.text[:max_chars // len(top)]
        label = r.ref_no or r.doc_id[:12]
        block = (
            f"[SOURCE {i}: {label}]\n"
            f"Folder: {r.virtual_folder or 'Uncategorized'}\n"
            f"---\n{snippet}\n"
        )
        context_blocks.append(block)

    context = "\n\n".join(context_blocks)

    system_msg = (
        "You are a precise legal research assistant. "
        "Answer the user's question using ONLY the provided case excerpts. "
        "Cite each source by its [SOURCE N] label whenever you draw on it. "
        "If the excerpts do not contain enough information, say so explicitly. "
        "Do not invent facts, holdings, or citations."
    )
    user_msg = (
        f"CASE EXCERPTS:\n\n{context}\n\n"
        f"QUESTION: {question}\n\n"
        "Provide a clear, structured answer with source citations."
    )

    answer = _chat([
        {"role": "system", "content": system_msg},
        {"role": "user",   "content": user_msg},
    ], cfg)

    sources = [
        {
            "doc_id": r.doc_id,
            "ref_no": r.ref_no,
            "virtual_folder": r.virtual_folder,
            "source_url": r.source_url,
        }
        for r in top
    ]
    return (answer, sources)


def analyze_case(
    db_path: str,
    doc_id: str,
    instruction: str,
) -> str:
    """Run any LLM instruction against a single indexed case.

    Examples of instruction:
        "Summarize the key holdings and reasoning."
        "Identify the strongest arguments for the appellant."
        "Find any weaknesses or dissents."
        "Compare this case to the doctrine of qualified immunity."
    """
    cfg = _load_llm_cfg()
    row = _load_one(db_path, doc_id)
    if not row:
        return f"Case {doc_id[:12]} not found in the database."

    label = row.ref_no or row.doc_id[:12]
    max_chars = cfg["max_context_chars"]
    text_excerpt = row.text[:max_chars]

    system_msg = (
        "You are an expert legal analyst. "
        "You will be given the text of a court opinion and a specific instruction. "
        "Respond accurately, referencing specific language from the text. "
        "Do not speculate beyond what the text supports."
    )
    user_msg = (
        f"CASE: {label}\n"
        f"FOLDER: {row.virtual_folder or 'Uncategorized'}\n\n"
        f"--- CASE TEXT (excerpt) ---\n{text_excerpt}\n--- END ---\n\n"
        f"INSTRUCTION: {instruction}"
    )

    return _chat([
        {"role": "system", "content": system_msg},
        {"role": "user",   "content": user_msg},
    ], cfg)


def semantic_search(
    db_path: str,
    query: str,
    top_k: int = 8,
) -> list[dict]:
    """Return top-k cases ranked by keyword overlap, each with an LLM relevance note.

    Returns a list of dicts:
        {doc_id, ref_no, virtual_folder, source_url, snippet, relevance_note}
    """
    cfg = _load_llm_cfg()
    rows = _load_all(db_path)
    if not rows:
        return []

    terms = set(_tokenize(query))
    top = _rank(rows, query, top_k)
    if not top:
        return []

    # Build a compact manifest for one LLM call (cheaper than N individual calls)
    manifest_lines: list[str] = []
    for i, r in enumerate(top, 1):
        snip = _snippet(r.text, terms)
        label = r.ref_no or r.doc_id[:12]
        manifest_lines.append(f"{i}. [{label}] {r.virtual_folder or ''} — {snip}")

    manifest = "\n".join(manifest_lines)
    system_msg = (
        "You are a legal research assistant. "
        "For each numbered case below, write ONE short sentence (≤25 words) "
        "explaining why it may be relevant to the user's query. "
        "Respond ONLY with a JSON array of strings, one per case, in the same order."
    )
    user_msg = f"QUERY: {query}\n\nCASES:\n{manifest}"

    notes: list[str] = []
    try:
        raw = _chat([
            {"role": "system", "content": system_msg},
            {"role": "user",   "content": user_msg},
        ], cfg)
        # Extract JSON array robustly
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            notes = json.loads(m.group())
        if not isinstance(notes, list):
            notes = []
    except Exception:
        notes = []

    # Pad / truncate notes to match top list
    while len(notes) < len(top):
        notes.append("")

    results: list[dict] = []
    for i, r in enumerate(top):
        results.append({
            "doc_id": r.doc_id,
            "ref_no": r.ref_no,
            "virtual_folder": r.virtual_folder,
            "source_url": r.source_url,
            "snippet": _snippet(r.text, terms, max_chars=300),
            "relevance_note": notes[i] if i < len(notes) else "",
        })
    return results
