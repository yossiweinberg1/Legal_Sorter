"""ID assignment and virtual folder categorization.

No files are physically copied or archived here -- in metadata-only mode
the only thing that persists is the database row (full text + tags).
build_virtual_folder just gives you a human-readable category string
for browsing/search results (e.g. "Jurisdiction_CA/2023/ContractLaw"),
even though nothing actually lives on disk at that path.
"""
import hashlib
import re
import unicodedata
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────
# Stop-words: terms that add no navigational value to folder names.
# Includes legal boilerplate, short noise words, and lone digits.
# ──────────────────────────────────────────────────────────────────────────────
_FOLDER_STOPWORDS: frozenset[str] = frozenset({
    # Legal boilerplate
    "affirmed", "reversed", "remanded", "dismissed", "submitted", "order",
    "opinion", "judgment", "appeal", "appeals", "appellate", "district",
    "circuit", "court", "courts", "hearing", "proceedings", "proceeding",
    "versus", "plaintiff", "defendant", "petitioner", "respondent",
    "filed", "decided", "argued", "argued", "re", "in", "on", "et", "al",
    # Generic English noise
    "the", "and", "for", "not", "with", "this", "that", "from",
    "have", "been", "were", "was", "are", "also", "any", "all",
    "other", "under", "upon", "into", "such", "case", "cases",
    # UI / HTML artefacts that leak in from web sources
    "span", "em", "class", "div", "href", "html", "li", "ul",
    "style", "text", "citation", "page", "document", "date", "volume", "section",
})


def compute_id(file_path: str) -> str:
    """SHA256 of file content. Doubles as automatic de-dup: if you pull the
    same case twice, it gets the same ID and is instantly recognized."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_virtual_folder(entities: dict, citations: list, keywords: list) -> str:
    parts = []
    jurisdiction = entities.get("JURISDICTION")
    if jurisdiction:
        parts.append(f"Jurisdiction_{_slug(jurisdiction[0])}")
    dates = entities.get("DATE")
    if dates:
        year = _extract_year(dates[0])
        if year:
            parts.append(year)

    # Use up to 3 cleaned keywords joined with underscores
    clean_kws = _clean_keywords(keywords)
    if clean_kws:
        parts.append("_".join(clean_kws[:3]))

    if not parts:
        # Fallback: Unsorted/YYYY or just Unsorted
        year_fallback = None
        if dates:
            year_fallback = _extract_year(dates[0])
        parts.append("Unsorted")
        if year_fallback:
            parts.append(year_fallback)

    return str(Path(*parts))


def _slug(s: str) -> str:
    """Convert *s* to a filesystem-safe ASCII slug.

    - Normalise Unicode via NFKD so accented chars become their base letter.
    - Preserve hyphens and underscores (meaningful in legal names).
    - Replace every other non-alphanumeric character with nothing.
    - Cap at 40 characters.
    """
    # NFKD decomposition: é → e + combining accent → only 'e' kept below
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_s = "".join(c for c in nfkd if not unicodedata.combining(c))
    slug = re.sub(r'[^\w\-]', '', ascii_s)  # keep word chars, hyphens
    slug = re.sub(r'_+', '_', slug).strip('_-')  # collapse runs
    return slug[:40] or "misc"


def _clean_keywords(keywords: list) -> list[str]:
    """Return a filtered, slug-ified list of up to 3 meaningful keywords."""
    result = []
    for kw in keywords:
        kw_str = str(kw).strip()
        kw_lower = kw_str.lower()
        # Skip blanks, pure numbers, single/double chars, stop-words
        if not kw_str or kw_lower in _FOLDER_STOPWORDS:
            continue
        if len(kw_str) <= 2:
            continue
        if re.fullmatch(r'[\d\W]+', kw_str):  # only digits / punctuation
            continue
        # Skip if it's just a year
        if re.fullmatch(r'(19|20)\d{2}', kw_str):
            continue
        slugged = _slug(kw_str)
        if slugged and slugged != "misc":
            result.append(slugged)
        if len(result) >= 3:
            break
    return result


def _extract_year(date_str: str) -> str | None:
    m = re.search(r"(19|20)\d{2}", date_str)
    return m.group() if m else None