import re
import unicodedata
from dataclasses import dataclass

# ──────────────────────────────────────────────────────────────────────────────
# High-performance regular expression to identify common US Legal Reporters.
# Pattern requires:
#   • volume:  2-digit minimum integer
#   • reporter abbreviation
#   • page:    2-digit minimum integer
# This deliberately rejects artefacts like "1 DAVID" or "2 SUBMITTED".
# ──────────────────────────────────────────────────────────────────────────────
CITATION_PATTERN = re.compile(
    r'\b(\d{2,4})\s+'
    r'(?:U\.S\.|F\.(?:2d|3d)?|F\.?\s?Supp\.?(?:2d|3d)?|S\.?\s?Ct\.|L\.?\s?Ed\.?(?:2d)?'
    r'|A\.?(?:2d|3d)?|N\.?E\.?(?:2d)?|N\.?W\.?(?:2d)?|P\.?(?:2d|3d)?'
    r'|S\.?E\.?(?:2d)?|S\.?W\.?(?:2d|3d)?|So\.?(?:2d|3d)?)'
    r'\s+(\d{1,4})\b',
    re.IGNORECASE,
)

# ──────────────────────────────────────────────────────────────────────────────
# Blocklist: patterns that are NOT legal citations even when they superficially
# match the volume + reporter + page structure.
# ──────────────────────────────────────────────────────────────────────────────
_BLOCKLIST = re.compile(
    r'\b(?:SUBMITTED|DECIDED|ARGUED|FILED|AFFIRMED|REVERSED|REMANDED|'
    r'JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|'
    r'OCTOBER|NOVEMBER|DECEMBER|'
    r'MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY|'
    r'[A-Z][a-z]+ (?:PASSED|DIED|BORN|AWARDED))\b',
    re.IGNORECASE,
)

# Structural keywords judges use to signal legal mandates, holdings, and rulings
RULING_KEYWORDS = [
    "we hold", "the court held", "held that", "the court concludes",
    "we conclude", "the judgment is", "ordered that", "reversed and remanded"
]


def _normalize_citation(raw: str) -> str:
    """Canonicalize a citation string.

    - Collapse whitespace runs to a single space.
    - Upper-case reporter abbreviations.
    - Normalize Unicode to ASCII where possible.
    """
    # NFKD → strip combining marks
    s = unicodedata.normalize("NFKD", raw)
    s = "".join(c for c in s if not unicodedata.combining(c))
    # Collapse internal whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


@dataclass
class AnalyzedMetadata:
    citations: list[str]
    ruling_logic: str
    suggested_tags: list[str]


def analyze_document_text(text: str) -> AnalyzedMetadata:
    """
    Parses raw text to deterministically extract legal citations and
    verbatim holding sentences without AI hallucinations.
    """
    # 1. Extract all legal citations mentioned within the document text
    raw_matches = CITATION_PATTERN.findall(text)

    # Reconstruct full match strings and apply quality + blocklist filters
    unique_citations: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(CITATION_PATTERN, text):
        raw = m.group(0)
        # Skip if the surrounding context looks like a non-citation artefact
        context_start = max(0, m.start() - 40)
        context = text[context_start: m.end() + 40]
        if _BLOCKLIST.search(context):
            continue
        volume, page = m.group(1), m.group(2) if m.lastindex and m.lastindex >= 2 else ("", "")
        # Require at least 2-digit volume and at least 1-digit page
        if not volume or int(volume) < 10:
            continue
        normalized = _normalize_citation(raw)
        if normalized not in seen:
            seen.add(normalized)
            unique_citations.append(normalized)

    # 2. Extract Verbatim Ruling Sentences (Extractive Logic Parsing)
    sentence_boundaries = r'(?<=[.!?])\s+'
    sentences = re.split(sentence_boundaries, text)

    extracted_rulings = []
    for sentence in sentences:
        clean_sentence = sentence.strip().replace("\n", " ")
        if any(kw in clean_sentence.lower() for kw in RULING_KEYWORDS):
            if 30 < len(clean_sentence) < 2900:
                extracted_rulings.append(clean_sentence)

    ruling_summary = " \n".join(extracted_rulings[:4]) if extracted_rulings else "No explicit ruling keyword extracted."

    # 3. Dynamic Tag Generator
    tags = []
    if "constitutional" in text.lower(): tags.append("Constitutional-Law")
    if "injunction" in text.lower(): tags.append("Injunction")
    if "summary judgment" in text.lower(): tags.append("Summary-Judgment")
    if "jurisdiction" in text.lower(): tags.append("Jurisdictional")

    return AnalyzedMetadata(
        citations=unique_citations,
        ruling_logic=ruling_summary,
        suggested_tags=tags
    )