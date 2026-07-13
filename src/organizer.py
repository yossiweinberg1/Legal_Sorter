"""ID assignment and virtual folder categorization.

No files are physically copied or archived here -- in metadata-only mode
the only thing that persists is the database row (full text + tags).
build_virtual_folder just gives you a human-readable category string
for browsing/search results (e.g. "Jurisdiction_CA/2023/ContractLaw"),
even though nothing actually lives on disk at that path.
"""
import hashlib
from pathlib import Path


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
    if keywords:
        parts.append(_slug(keywords[0]))
    if not parts:
        parts.append("Unsorted")
    return str(Path(*parts))


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "" for c in s)[:40].strip("") or "misc"


def _extract_year(date_str: str) -> str | None:
    import re
    m = re.search(r"(19|20)\d{2}", date_str)
    return m.group() if m else None