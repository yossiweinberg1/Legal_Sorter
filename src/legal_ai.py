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
        "fast_model": llm.get("fast_model", llm.get("model", "gpt-4o-mini")),
        "accurate_model": llm.get("accurate_model", llm.get("model", "gpt-4o-mini")),
        "max_context_chars": int(llm.get("max_context_chars", 12000)),
        "require_citations": bool(llm.get("require_citations", True)),
        "min_sources": int(llm.get("min_sources", 1)),
        "timeout_seconds": int(llm.get("timeout_seconds", 60)),
    }


# ---------------------------------------------------------------------------
# Low-level HTTP call (avoids adding openai SDK as a hard dep)
# ---------------------------------------------------------------------------

def _chat(messages: list[dict], cfg: dict, *, model: str | None = None, temperature: float = 0.2) -> str:
    """POST to any OpenAI-compatible /chat/completions endpoint."""
    import urllib.request

    payload = json.dumps({
        "model": model or cfg["model"],
        "messages": messages,
        "temperature": temperature,
    }).encode()
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]

    url = f"{cfg['base_url']}/chat/completions"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=int(cfg.get("timeout_seconds", 60))) as resp:
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
    barcode: str | None = None
    barcode_confidence: float | None = None
    keywords: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    subsequent_history: list[dict] = field(default_factory=list)


_MAX_LOAD = 2000  # max rows scanned for keyword ranking


def _load_all(db_path: str, barcode_prefix: str | None = None) -> list[_CaseRow]:
    """Load documents from the database, with optional barcode filtering.

    Args:
        db_path:        Path to the SQLite database.
        barcode_prefix: Restricts results to documents whose barcode matches
                        this filter.  Two forms are accepted:

                        **Plain prefix** (no ``%`` in the string):
                          A literal prefix like ``"LS-CA-CA9-"``; the function
                          escapes any LIKE specials and appends ``%`` automatically.

                        **Raw LIKE pattern** (contains ``%``):
                          A pattern produced by ``barcode.barcode_prefix()``, e.g.
                          ``"LS-%-CA9-%-%-"``.  Passed to SQL as-is with no further
                          escaping or modification.

                        Pass ``None`` to load all documents (up to _MAX_LOAD).
    """
    rows: list[_CaseRow] = []
    try:
        with sqlite3.connect(db_path) as conn:
            if barcode_prefix is not None:
                if "%" in barcode_prefix:
                    # Raw wildcard pattern from barcode_prefix() — use as-is
                    like_pattern = barcode_prefix
                else:
                    # Plain prefix — escape LIKE specials, then append %
                    like_pattern = (
                        barcode_prefix
                        .replace("\\", "\\\\")
                        .replace("%", "\\%")
                        .replace("_", "\\_")
                        + "%"
                    )
                cur = conn.execute(
                    """SELECT id, ref_no, virtual_folder, source_url, text,
                              keywords_json, citations_json, barcode, barcode_confidence
                       FROM documents
                       WHERE text IS NOT NULL AND text != ''
                         AND (content_source IS NULL OR content_source != 'snippet_only')
                         AND (sanity_check_passed IS NULL OR sanity_check_passed != 0)
                         AND barcode LIKE ? ESCAPE '\\'
                       ORDER BY added_at DESC
                       LIMIT ?""",
                    (like_pattern, _MAX_LOAD),
                )
            else:
                cur = conn.execute(
                    """SELECT id, ref_no, virtual_folder, source_url, text,
                              keywords_json, citations_json, barcode, barcode_confidence
                       FROM documents
                       WHERE text IS NOT NULL AND text != ''
                         AND (content_source IS NULL OR content_source != 'snippet_only')
                         AND (sanity_check_passed IS NULL OR sanity_check_passed != 0)
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
                    barcode=r[7],
                    barcode_confidence=r[8],
                ))
    except Exception as exc:
        log.warning("DB load failed: %s", exc)
    return rows


def _load_one(db_path: str, doc_id: str) -> _CaseRow | None:
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                """SELECT id, ref_no, virtual_folder, source_url, text,
                          keywords_json, citations_json, barcode, barcode_confidence
                   FROM documents WHERE id = ?
                     AND (content_source IS NULL OR content_source != 'snippet_only')
                     AND (sanity_check_passed IS NULL OR sanity_check_passed != 0)""",
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
                barcode=r[7],
                barcode_confidence=r[8],
            )
    except Exception as exc:
        log.warning("DB load_one failed: %s", exc)
        return None


def _load_one_by_barcode(db_path: str, barcode: str) -> _CaseRow | None:
    """Fetch a single document by its exact structured barcode ID."""
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                """SELECT id, ref_no, virtual_folder, source_url, text,
                          keywords_json, citations_json, barcode, barcode_confidence
                   FROM documents WHERE barcode = ?
                     AND (content_source IS NULL OR content_source != 'snippet_only')
                     AND (sanity_check_passed IS NULL OR sanity_check_passed != 0)""",
                (barcode,),
            )
            r = cur.fetchone()
            if not r:
                return None
            return _CaseRow(
                doc_id=r[0], ref_no=r[1], virtual_folder=r[2],
                source_url=r[3], text=r[4] or "",
                keywords=_safe_json(r[5], []),
                citations=_safe_json(r[6], []),
                barcode=r[7],
                barcode_confidence=r[8],
            )
    except Exception as exc:
        log.warning("DB load_one failed: %s", exc)
        return None


def _safe_json(raw: Any, fallback: Any) -> Any:
    try:
        return json.loads(raw) if raw else fallback
    except Exception:
        return fallback


def _load_subsequent_history(db_path: str, doc_id: str, limit: int = 5) -> list[dict]:
    try:
        from .database import DB
    except ImportError:
        from database import DB  # type: ignore
    db = DB(db_path)
    try:
        return db.get_subsequent_history(doc_id, limit=limit)
    finally:
        try:
            db.conn.close()
        except Exception:
            pass


def _load_subsequent_history_map(db_path: str, doc_ids: list[str], limit: int = 5) -> dict[str, list[dict]]:
    try:
        from .database import DB
    except ImportError:
        from database import DB  # type: ignore
    db = DB(db_path)
    try:
        summary = db.get_subsequent_history_summary_map(doc_ids, limit=limit)
        return {doc_id: data.get("items", []) for doc_id, data in summary.items()}
    finally:
        try:
            db.conn.close()
        except Exception:
            pass


def _subsequent_history_context(items: list[dict]) -> str:
    if not items:
        return "No later citing cases are recorded in the archive."
    lines = []
    for item in items[:5]:
        label = item.get("ref_no") or (item.get("doc_id", "")[:12] + "…")
        barcode = item.get("barcode") or "no-barcode"
        year = item.get("year") or "unknown-year"
        treatment = item.get("treatment") or "cited"
        context = (item.get("context") or "").strip()
        line = f"- {label} / {barcode} / {year} / {treatment}"
        if context:
            line += f": {context[:180]}"
        lines.append(line)
    return "\n".join(lines)


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


def _extractive_fallback(question: str, rows: list[_CaseRow]) -> str:
    parts = [f"Unable to complete an LLM answer for: {question}"]
    parts.append("Relevant source-backed excerpts:")
    for idx, row in enumerate(rows, 1):
        label = row.ref_no or row.doc_id[:12]
        parts.append(f"[SOURCE {idx}: {label}] {_snippet(row.text, set(_tokenize(question)), max_chars=220)}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def query_cases(
    db_path: str,
    question: str,
    top_k: int = 5,
    barcode_prefix: str | None = None,
) -> tuple[str, list[dict]]:
    """Ask a legal question; answer is grounded in the top-k indexed cases.

    Args:
        db_path:        Path to the SQLite database.
        question:       The legal research question.
        top_k:          Number of cases to retrieve and summarise.
        barcode_prefix: Optional structured ID prefix for pre-filtering.
                        E.g. ``"LS-CA-CA9-"`` restricts the search to
                        9th Circuit cases only before any ranking or LLM
                        work is done.

    Uses a two-pass RAG approach so the LLM always reasons over legally
    meaningful content instead of a fixed character slice:

      Pass 1 — For each of the top-k cases, send the full case text to the
               LLM with a single instruction: extract the holding, key facts,
               and any relevant citations in 3-5 sentences.  This produces a
               dense, high-signal summary of each case.

      Pass 2 — Send all summaries together to the LLM to answer the user's
               question with full source citations.

    Returns:
        (answer_str, sources_list)
        sources_list entries: {doc_id, ref_no, barcode, virtual_folder, source_url}
    """
    cfg = _load_llm_cfg()
    rows = _load_all(db_path, barcode_prefix=barcode_prefix)
    if not rows:
        if barcode_prefix:
            return (
                f"No indexed documents found matching the prefix '{barcode_prefix}'. "
                "Try broadening the filter or removing it.",
                [],
            )
        return ("No indexed documents found. Ingest cases first.", [])

    top = _rank(rows, question, top_k)
    if not top:
        return ("No relevant cases found for that query. Try different search terms.", [])
    if len(top) < max(1, int(cfg.get("min_sources", 1))):
        return ("I don't have enough grounded sources in the archive to answer that reliably.", [])

    # --- Pass 1: summarise each case from its full text ---
    history_map = _load_subsequent_history_map(db_path, [r.doc_id for r in top], limit=4)
    summarize_system = (
        "You are a precise legal analyst. "
        "Given the full text of a court opinion, extract ONLY: "
        "(1) the core holding in one sentence, "
        "(2) the key facts in 1-2 sentences, "
        "(3) any citations to other cases mentioned. "
        "Be concise and use only what the text actually says."
    )

    summaries: list[str] = []
    for r in top:
        label = r.ref_no or r.doc_id[:12]
        # Include barcode in the source label so the LLM can reason about
        # court/jurisdiction/topic from the ID alone.
        bc_label = f"{label} / {r.barcode}" if r.barcode else label
        user_msg = (
            f"CASE: {bc_label}\n"
            f"FOLDER: {r.virtual_folder or 'Uncategorized'}\n\n"
            f"--- SUBSEQUENT HISTORY ---\n{_subsequent_history_context(history_map.get(r.doc_id, r.subsequent_history[:4]))}\n--- END SUBSEQUENT HISTORY ---\n\n"
            f"--- FULL CASE TEXT ---\n{r.text}\n--- END ---\n\n"
            "Extract the holding, key facts, and citations as instructed."
        )
        try:
            summary = _chat([
                {"role": "system", "content": summarize_system},
                {"role": "user",   "content": user_msg},
            ], cfg, model=cfg.get("fast_model"), temperature=0.0)
        except Exception as exc:
            log.warning("Pass-1 summary failed for %s: %s", label, exc)
            summary = f"(Summary unavailable for {label})"
        summaries.append(f"[SOURCE {top.index(r) + 1}: {bc_label}]\n{summary}")

    # --- Pass 2: answer the question from the summaries ---
    context = "\n\n".join(summaries)
    answer_system = (
        "You are a precise legal research assistant. "
        "Answer the user's question using ONLY the provided case summaries. "
        "Cite each source by its [SOURCE N] label whenever you draw on it. "
        "If the summaries do not contain enough information, say so explicitly. "
        "Do not invent facts, holdings, or citations. "
        "If support is weak or ambiguous, refuse and explain that the archive is insufficient."
    )
    answer_user = (
        f"CASE SUMMARIES:\n\n{context}\n\n"
        f"QUESTION: {question}\n\n"
        "Provide a clear, structured answer with source citations."
    )

    try:
        answer = _chat([
            {"role": "system", "content": answer_system},
            {"role": "user",   "content": answer_user},
        ], cfg, model=cfg.get("accurate_model"))
    except Exception:
        answer = _extractive_fallback(question, top)

    if cfg.get("require_citations", True):
        cited = re.findall(r"\[SOURCE\s+\d+\]", answer)
        if not cited:
            answer = (
                "I can't provide a grounded answer because the generated response lacked source citations. "
                "Please review the retrieved sources directly."
            )
        elif len(set(cited)) < min(len(top), max(1, int(cfg.get("min_sources", 1)))):
            answer = (
                "I can't provide a grounded answer because too few retrieved sources supported it. "
                "Please refine the query or inspect the listed sources directly."
            )

    sources = [
        {
            "doc_id": r.doc_id,
            "ref_no": r.ref_no,
            "barcode": r.barcode,
            "barcode_confidence": r.barcode_confidence,
            "virtual_folder": r.virtual_folder,
            "source_url": r.source_url,
            "retrieval_score": _score(r, set(_tokenize(question))),
            "source_preview": (r.text or "")[:2000],
            "source_text": (r.text or "")[:12000],
            "source_text_truncated": len(r.text or "") > 12000,
            "citations": r.citations,
            "subsequent_history": history_map.get(r.doc_id, r.subsequent_history[:3]),
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

    Sends the full stored case text — not a truncated excerpt — so the LLM
    has complete context to reason from.

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
    bc_label = f"{label} / {row.barcode}" if row.barcode else label
    history_items = _load_subsequent_history_map(db_path, [row.doc_id], limit=5).get(row.doc_id, [])

    system_msg = (
        "You are an expert legal analyst. "
        "You will be given the full text of a court opinion and a specific instruction. "
        "Respond accurately, referencing specific language from the text. "
        "Do not speculate beyond what the text supports."
    )
    user_msg = (
        f"CASE: {bc_label}\n"
        f"FOLDER: {row.virtual_folder or 'Uncategorized'}\n\n"
        f"--- SUBSEQUENT HISTORY ---\n{_subsequent_history_context(history_items)}\n--- END SUBSEQUENT HISTORY ---\n\n"
        f"--- FULL CASE TEXT ---\n{row.text}\n--- END ---\n\n"
        f"INSTRUCTION: {instruction}"
    )

    return _chat([
        {"role": "system", "content": system_msg},
        {"role": "user",   "content": user_msg},
    ], cfg)


def analyze_case_by_barcode(
    db_path: str,
    barcode: str,
    instruction: str,
) -> str:
    """Run any LLM instruction against a case located by its structured barcode ID.

    This allows the LLM itself (or any caller) to request a specific document
    using the human-readable structured ID (e.g. ``"LS-CA-CA9-CIV-2019-000042"``)
    rather than the opaque SHA256.

    Returns the LLM response string, or an error message if the barcode is not found.
    """
    cfg = _load_llm_cfg()
    row = _load_one_by_barcode(db_path, barcode)
    if not row:
        return (
            f"No document found with barcode '{barcode}'. "
            "Check the ID or use semantic_search to find the correct document."
        )
    return analyze_case(db_path, row.doc_id, instruction)


def semantic_search(
    db_path: str,
    query: str,
    top_k: int = 8,
    barcode_prefix: str | None = None,
) -> list[dict]:
    """Return top-k cases ranked by keyword overlap, each with an LLM relevance note.

    Args:
        db_path:        Path to the SQLite database.
        query:          The search query string.
        top_k:          Number of results to return.
        barcode_prefix: Optional structured ID prefix.  When given, only
                        documents matching the prefix are considered before
                        keyword ranking.  E.g. ``"LS-ST-TEX-"`` for Texas
                        state-court cases only.

    Returns a list of dicts:
        {doc_id, ref_no, barcode, virtual_folder, source_url, snippet, relevance_note}
    """
    cfg = _load_llm_cfg()
    rows = _load_all(db_path, barcode_prefix=barcode_prefix)
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
        bc_label = f"{label} / {r.barcode}" if r.barcode else label
        manifest_lines.append(f"{i}. [{bc_label}] {r.virtual_folder or ''} — {snip}")

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
            "barcode": r.barcode,
            "virtual_folder": r.virtual_folder,
            "source_url": r.source_url,
            "snippet": _snippet(r.text, terms, max_chars=300),
            "relevance_note": notes[i] if i < len(notes) else "",
        })
    return results
