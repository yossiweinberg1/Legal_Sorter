from __future__ import annotations

import re

TREATMENT_UNKNOWN = "unknown"
TREATMENT_CITED = "cited"
TREATMENT_FOLLOWED = "followed"
TREATMENT_DISTINGUISHED = "distinguished"
TREATMENT_CRITICIZED = "criticized"
TREATMENT_OVERRULED = "overruled"
TREATMENT_LIMITED = "limited"

_TREATMENT_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    (
        TREATMENT_OVERRULED,
        (
            r"\boverrul(?:ed|es|ing)\b",
            r"\babrogat(?:ed|es|ing)\b",
            r"\bno longer good law\b",
        ),
    ),
    (
        TREATMENT_LIMITED,
        (
            r"\blimit(?:ed|s|ing)\b",
            r"\bnarrow(?:ed|s|ing)\b",
            r"\bconfine(?:d|s|ment)\b",
        ),
    ),
    (
        TREATMENT_DISTINGUISHED,
        (
            r"\bdistinguish(?:ed|es|ing)\b",
            r"\bdifferent from\b",
            r"\bnot controlling\b",
        ),
    ),
    (
        TREATMENT_CRITICIZED,
        (
            r"\bcriticiz(?:ed|es|ing)\b",
            r"\bquestion(?:ed|s|ing)\b",
            r"\breject(?:ed|s|ing)\b",
            r"\bdeclin(?:ed|es|ing) to follow\b",
        ),
    ),
    (
        TREATMENT_FOLLOWED,
        (
            r"\bfollow(?:ed|s|ing)\b",
            r"\bappl(?:ied|ies|ying)\b",
            r"\brelied on\b",
            r"\bcontrolled by\b",
        ),
    ),
]


def normalize_citation(citation: str) -> str:
    raw = (citation or "").strip().upper()
    if not raw:
        return ""
    raw = raw.replace("–", "-").replace("—", "-")
    raw = re.sub(r"\s+", " ", raw)
    raw = re.sub(r"\b([A-Z])\.\s+(?=[A-Z]\.)", r"\1.", raw)
    raw = re.sub(r"\s+([,.;:)])", r"\1", raw)
    raw = re.sub(r"([(\[]) +", r"\1", raw)
    return raw.strip()


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start), text.rfind(";", 0, start))
    right_candidates = [idx for idx in (text.find(".", end), text.find("\n", end), text.find(";", end)) if idx != -1]
    right = min(right_candidates) if right_candidates else len(text)
    return max(0, left + 1), right if right > 0 else len(text)


def detect_treatment(context: str) -> str:
    haystack = (context or "").lower()
    if not haystack.strip():
        return TREATMENT_UNKNOWN
    for treatment, patterns in _TREATMENT_PATTERNS:
        if any(re.search(pattern, haystack) for pattern in patterns):
            return treatment
    if re.search(r"\bcit(?:e|ed|es|ing)\b", haystack):
        return TREATMENT_CITED
    return TREATMENT_UNKNOWN


def extract_citation_relationships(
    text: str,
    citations: list[str],
    *,
    self_citation: str | None = None,
) -> list[dict]:
    found: dict[str, dict] = {}
    seen_order: list[str] = []
    source_text = text or ""
    self_key = normalize_citation(self_citation or "")
    for citation in citations or []:
        citation_key = normalize_citation(citation)
        if not citation_key or citation_key == self_key:
            continue
        entry = found.get(citation_key)
        if entry is None:
            entry = {
                "citation": citation,
                "citation_key": citation_key,
                "treatment": TREATMENT_UNKNOWN,
                "context": "",
            }
            found[citation_key] = entry
            seen_order.append(citation_key)
        matches = list(re.finditer(re.escape(citation), source_text))
        if not matches:
            if entry["treatment"] == TREATMENT_UNKNOWN:
                entry["treatment"] = TREATMENT_CITED
            continue
        for match in matches[:5]:
            left, right = _sentence_bounds(source_text, match.start(), match.end())
            context = source_text[left:right].strip().replace("\n", " ")
            treatment = detect_treatment(context)
            if entry["treatment"] in {TREATMENT_UNKNOWN, TREATMENT_CITED} and treatment != TREATMENT_UNKNOWN:
                entry["treatment"] = treatment
                entry["context"] = context[:280]
            elif not entry["context"]:
                entry["context"] = context[:280]
        if entry["treatment"] == TREATMENT_UNKNOWN:
            entry["treatment"] = TREATMENT_CITED
    return [found[key] for key in seen_order]
