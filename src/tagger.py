"""Tag generation -- entirely extractive, entirely regex/gazetteer based.
No ML model, no spaCy (its dependency 'blis' has no Windows ARM64 build --
this approach sidesteps that completely). Every tag traces back to text
that literally appears in the document, so there is nothing to invent.

Layers:
  1. Legal citation regex      -> case cites, statutes
  2. Case caption regex        -> party names (e.g. "Smith v. Jones")
  3. Date regex                -> filing/decision dates
  4. Jurisdiction gazetteer    -> matches against known US courts/states
  5. TF-IDF keywords           -> terms that make THIS doc distinctive
     vs. the rest of your corpus (grows more precise as your archive grows)
"""
import re
from dataclasses import dataclass, field

CITATION_PATTERNS = [
    r"\b\d{1,4}\s+[A-Z][A-Za-z.\s]{1,20}\d{1,4}(?:\s*\(\d{4}\))?",  # "410 U.S. 113 (1973)"
    r"\b\d+\s+U\.S\.C\.\s*§\s\d+[a-zA-Z]*",                        # "42 U.S.C. § 1983"
    r"\b\d+\s+C\.F\.R\.\s*§\s\d+(\.\d+)*",                          # "29 C.F.R. 1601.1"
]

CASE_CAPTION_PATTERNS = [
    r"\b([A-Z][A-Za-z.,&\'\s]{2,60}?)\s+v\.?\s+([A-Z][A-Za-z.,&\'\s]{2,60}?)(?=[,\n]|\s+\d|\s+\()",
    r"\bIn\s+re\s+([A-Z][A-Za-z.,&\'\s]{2,60}?)(?=[,\n]|\s+\d)",
]

DATE_PATTERNS = [
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b",
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
]

US_STATES = [
    "Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut",
    "Delaware","Florida","Georgia","Hawaii","Idaho","Illinois","Indiana","Iowa",
    "Kansas","Kentucky","Louisiana","Maine","Maryland","Massachusetts","Michigan",
    "Minnesota","Mississippi","Missouri","Montana","Nebraska","Nevada",
    "New Hampshire","New Jersey","New Mexico","New York","North Carolina",
    "North Dakota","Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island",
    "South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont",
    "Virginia","Washington","West Virginia","Wisconsin","Wyoming",
    "District of Columbia",
]

COURT_KEYWORDS = [
    "Supreme Court", "Court of Appeals", "District Court", "Circuit Court",
    "Superior Court", "Federal Circuit", "Bankruptcy Court", "Family Court",
    "First Circuit", "Second Circuit", "Third Circuit", "Fourth Circuit",
    "Fifth Circuit", "Sixth Circuit", "Seventh Circuit", "Eighth Circuit",
    "Ninth Circuit", "Tenth Circuit", "Eleventh Circuit", "D.C. Circuit",
]


@dataclass
class Tags:
    entities: dict = field(default_factory=dict)
    citations: list = field(default_factory=list)
    keywords: list = field(default_factory=list)


def extract_citations(text: str) -> list[str]:
    found = set()
    for pattern in CITATION_PATTERNS:
        for m in re.finditer(pattern, text):
            found.add(m.group().strip())
    return sorted(found)


def extract_case_parties(text: str, max_matches: int = 5) -> list[str]:
    found = []
    for pattern in CASE_CAPTION_PATTERNS:
        for m in re.finditer(pattern, text[:5000]):
            found.append(m.group().strip())
            if len(found) >= max_matches:
                return found
    return found


def extract_dates(text: str, max_matches: int = 10) -> list[str]:
    found = set()
    for pattern in DATE_PATTERNS:
        for m in re.finditer(pattern, text):
            found.add(m.group().strip())
    return sorted(found)[:max_matches]


def extract_jurisdiction(text: str) -> list[str]:
    found = []
    for state in US_STATES:
        if re.search(rf"\b{re.escape(state)}\b", text):
            found.append(state)
    for court in COURT_KEYWORDS:
        if court in text:
            found.append(court)
    return found


def extract_entities(text: str) -> dict:
    buckets = {
        "PARTIES": extract_case_parties(text),
        "DATE": extract_dates(text),
        "JURISDICTION": extract_jurisdiction(text),
    }
    return {k: v for k, v in buckets.items() if v}

def extract_self_citation(text: str, citations: list[str]) -> str | None:
    """A case's OWN citation almost always appears earliest in the
    document, in the caption/heading. Picks whichever already-extracted
    citation occurs first in the text."""
    if not citations:
        return None
    best, best_pos = None, len(text) + 1
    for c in citations:
        pos = text.find(c)
        if 0 <= pos < best_pos:
            best, best_pos = c, pos
    return best


def extract_keywords_tfidf(text: str, corpus_texts: list[str], top_n: int = 8) -> list[str]:
    from sklearn.feature_extraction.text import TfidfVectorizer

    docs = corpus_texts + [text]
    if len(docs) < 3:
        return _top_frequency_terms(text, top_n)

    vectorizer = TfidfVectorizer(stop_words="english", max_features=5000, ngram_range=(1, 2))
    matrix = vectorizer.fit_transform(docs)
    row = matrix[-1].toarray().flatten()
    terms = vectorizer.get_feature_names_out()
    top_idx = row.argsort()[::-1][:top_n]
    return [terms[i] for i in top_idx if row[i] > 0]


def _top_frequency_terms(text: str, top_n: int) -> list[str]:
    words = re.findall(r"[A-Za-z]{4,}", text.lower())
    from collections import Counter
    common = Counter(words).most_common(top_n)
    return [w for w, _ in common]


def tag_document(text: str, corpus_texts: list[str], max_keywords: int = 8) -> Tags:
    return Tags(
        entities=extract_entities(text),
        citations=extract_citations(text),
        keywords=extract_keywords_tfidf(text, corpus_texts, max_keywords),
    )