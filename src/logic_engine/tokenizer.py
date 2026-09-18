from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int
    kind: str


# Groups are deliberately small and deterministic. Word tokens are also emitted,
# so no word disappears just because it participates in a group.
_GROUP_PATTERNS = (
    ("money", re.compile(r"\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?")),
    ("date", re.compile(r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s+\d{4}\b", re.I)),
    ("date", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")),
    ("name", re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Judge|Justice)\.\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b")),
    ("address", re.compile(r"\b\d{1,5}\s+[A-Z][A-Za-z]+(?:\s+[A-Za-z]+){0,4}\s+(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Drive|Dr)\.?\b")),
)
_WORD_PATTERN = re.compile(r"\b[\w]+(?:[-'][\w]+)*\b", re.UNICODE)


def tokenize(document: str) -> list[Token]:
    """Return every word plus recognized groups, ordered by source position.

    Groups intentionally overlap their component words. This preserves the
    requirement that every word is analyzed while also exposing useful phrases.
    """
    tokens = [Token(m.group(), m.start(), m.end(), "word") for m in _WORD_PATTERN.finditer(document)]
    for kind, pattern in _GROUP_PATTERNS:
        tokens.extend(Token(m.group(), m.start(), m.end(), "group") for m in pattern.finditer(document))
    return sorted(tokens, key=lambda token: (token.start, token.end, token.kind == "word"))
