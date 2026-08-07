import json
import re
import sqlite3
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAX_LOAD_DOCS = 1500
NO_DOCS_SENTINEL = "No indexed documents available. Ingest cases first, then retry."
NO_MATCH_SENTINEL = "No relevant source-backed matches found for that prompt. Try narrower legal terms."


@dataclass
class StudyDoc:
    doc_id: str
    ref_no: str | None
    source_url: str | None
    virtual_folder: str | None
    text: str
    ruling_logic: str
    citations: list[str]
    keywords: list[str]


def _safe_json(raw: str, fallback):
    try:
        return json.loads(raw) if raw else fallback
    except Exception:
        return fallback


def _extract_ruling_logic(entities_json: str) -> str:
    entities = _safe_json(entities_json, {})
    if isinstance(entities, dict):
        value = entities.get("RULING_LOGIC")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "No explicit ruling logic extracted."


def _load_docs(db_path: str) -> list[StudyDoc]:
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, ref_no, source_url, virtual_folder, text, entities_json, citations_json, keywords_json
                FROM documents
                WHERE text IS NOT NULL AND text != ''
                ORDER BY added_at DESC
                LIMIT ?
                """,
                (MAX_LOAD_DOCS,),
            )
            rows = cur.fetchall()
    except Exception as e:
        log.warning("Failed to load study documents from database: %s", e)
        return []

    docs = []
    for row in rows:
        docs.append(
            StudyDoc(
                doc_id=row[0],
                ref_no=row[1],
                source_url=row[2],
                virtual_folder=row[3],
                text=row[4] or "",
                ruling_logic=_extract_ruling_logic(row[5]),
                citations=_safe_json(row[6], []),
                keywords=_safe_json(row[7], []),
            )
        )
    return docs


def _terms(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z]{3,}", (text or "").lower())


def _score_doc(doc: StudyDoc, query_terms: set[str], selected_doc_id: str | None) -> float:
    hay = " ".join(
        [
            doc.virtual_folder or "",
            doc.ruling_logic or "",
            " ".join(doc.keywords or []),
            " ".join(doc.citations or []),
            doc.text[:6000],
        ]
    ).lower()
    score = 0.0
    for term in query_terms:
        if term in hay:
            score += 1.0
            score += min(hay.count(term), 3) * 0.2
    if selected_doc_id and doc.doc_id == selected_doc_id:
        score += 4.0
    return score


def _top_quote(text: str, query_terms: set[str]) -> str:
    chunks = re.split(r"(?<=[.!?])\s+", text[:12000])
    best = ""
    best_score = -1
    for chunk in chunks:
        cleaned = chunk.strip().replace("\n", " ")
        if len(cleaned) < 50:
            continue
        low = cleaned.lower()
        score = sum(1 for t in query_terms if t in low)
        if score > best_score:
            best_score = score
            best = cleaned
    if not best:
        best = text[:320].replace("\n", " ").strip()
    return best[:360]


def _source_line(doc: StudyDoc) -> str:
    return f"- {doc.ref_no or 'N/A'} | {doc.doc_id[:12]} | {doc.source_url or 'No source URL'}"


def _detect_mode(prompt: str) -> str:
    p = prompt.lower()
    if "irac" in p or "issue rule application conclusion" in p:
        return "irac"
    if "flashcard" in p or "cold call" in p:
        return "flashcards"
    if "brief" in p:
        return "brief"
    return "qa"


def _build_brief(prompt: str, docs: list[StudyDoc], query_terms: set[str]) -> str:
    lead = docs[0]
    quote = _top_quote(lead.text, query_terms)
    return (
        "STUDY BRIEF (Citation-Backed)\n"
        f"Prompt: {prompt}\n\n"
        f"Case Focus: {lead.ref_no or lead.doc_id[:12]}\n"
        f"Virtual Folder: {lead.virtual_folder or 'Uncategorized'}\n"
        f"Ruling Logic: {lead.ruling_logic}\n"
        f"Key Quote: \"{quote}\"\n"
    )


def _build_irac(prompt: str, docs: list[StudyDoc], query_terms: set[str]) -> str:
    lead = docs[0]
    quote = _top_quote(lead.text, query_terms)
    return (
        "IRAC DRILL (Citation-Backed)\n"
        f"Issue: Based on '{prompt}', identify the governing issue from {lead.ref_no or lead.doc_id[:12]}.\n"
        f"Rule: {lead.ruling_logic}\n"
        f"Application: Compare that rule against the quoted facts/holding: \"{quote}\"\n"
        "Conclusion: Draft a one-paragraph outcome prediction and verify each claim against sources below.\n"
    )


def _build_flashcards(docs: list[StudyDoc], query_terms: set[str]) -> str:
    cards = ["COLD-CALL FLASHCARDS (Citation-Backed)"]
    for idx, doc in enumerate(docs[:3], 1):
        quote = _top_quote(doc.text, query_terms)
        cards.append(
            f"\nCard {idx}\n"
            f"Q: What holding from {doc.ref_no or doc.doc_id[:12]} is most exam-relevant?\n"
            f"A: {doc.ruling_logic}\n"
            f"Evidence Quote: \"{quote}\""
        )
    return "\n".join(cards)


def _build_qa(prompt: str, docs: list[StudyDoc], query_terms: set[str]) -> str:
    lines = ["GROUNDED ANSWER (Citation-Backed)"]
    lines.append(f"Question: {prompt}\n")
    for doc in docs[:3]:
        quote = _top_quote(doc.text, query_terms)
        lines.append(
            f"- From {doc.ref_no or doc.doc_id[:12]}: {doc.ruling_logic}\n"
            f"  Quote: \"{quote}\""
        )
    lines.append("\nSynthesis: Compare the rules above and resolve conflicts using jurisdiction/date priority.")
    return "\n".join(lines)


def generate_study_response(db_path: str, prompt: str, selected_doc_id: str | None = None, max_sources: int = 4) -> str:
    """Generate source-grounded study output from indexed documents.

    Args:
        db_path: Absolute or relative path to the SQLite database.
        prompt: User prompt used for retrieval scoring and output mode detection.
        selected_doc_id: Optional active document ID to boost retrieval ranking.
        max_sources: Maximum number of documents to cite in the final response.

    Returns:
        A citation-backed study response string, or one of the sentinel values
        `NO_DOCS_SENTINEL` / `NO_MATCH_SENTINEL` when grounded output is unavailable.
    """
    docs = _load_docs(db_path)
    if not docs:
        return NO_DOCS_SENTINEL

    query_terms = set(_terms(prompt))
    scored = sorted(
        ((doc, _score_doc(doc, query_terms, selected_doc_id)) for doc in docs),
        key=lambda x: x[1],
        reverse=True,
    )
    top_docs = [doc for doc, score in scored if score > 0][:max_sources]
    if not top_docs:
        return NO_MATCH_SENTINEL
    mode = _detect_mode(prompt)

    if mode == "brief":
        body = _build_brief(prompt, top_docs, query_terms)
    elif mode == "irac":
        body = _build_irac(prompt, top_docs, query_terms)
    elif mode == "flashcards":
        body = _build_flashcards(top_docs, query_terms)
    else:
        body = _build_qa(prompt, top_docs, query_terms)

    source_block = "\n".join(_source_line(d) for d in top_docs)
    return (
        f"{body}\n\n"
        "Sources\n"
        f"{source_block}\n\n"
        "Study aid only, not legal advice."
    )
