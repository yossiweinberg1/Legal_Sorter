"""Structured Hierarchical ID ("Smart Barcode") for LegalSorter documents.

Format
------
    LS-{CT}-{JR}-{SM}-{YR}-{SQ}

Segments
--------
    LS   Namespace prefix – always literal "LS"
    CT   Case type     2 chars  e.g. SC, CA, DC, ST, SB, BR, OT
    JR   Jurisdiction  2-4 chars  e.g. US, CA9, NYS, TEX, UNK
    SM   Subject matter 3 chars  e.g. CON, CRM, CIV, CTR, TRT, FAM, IMM, OTH
    YR   Decision year  4 digits  e.g. 2019  (0000 when unknown)
    SQ   Sequence       6 digits  ties back to the existing LC-XXXXXX ref_no

Example IDs
-----------
    LS-SC-US-CON-2022-000128   # U.S. Supreme Court, constitutional law, 2022
    LS-CA-CA9-CIV-2019-000042  # Ninth Circuit, civil rights, 2019
    LS-DC-NYS-CRM-2021-000007  # S.D.N.Y. district court, criminal, 2021
    LS-ST-TEX-FAM-2020-000315  # Texas state court, family law, 2020
    LS-SB-US-OTH-2018-000003   # Federal statute, 2018
    LS-BR-UNK-OTH-0000-000099  # Brief, jurisdiction/year unknown

Generation strategies (controlled by config.barcode.strategy)
--------------------------------------------------------------
    "rules"            Pure regex/gazetteer, always available, no LLM needed.
    "llm"              Ask a small local LLM; raise on failure.
    "llm_with_fallback" Try LLM first; fall back to rules on any error (default).

The rule engine is authoritative for CT, JR, and YR; the LLM adds value for SM
(subject matter) because legal topic classification benefits from reading the text.

Prefix patterns
---------------
    barcode_prefix() returns a raw SQL LIKE pattern (e.g. ``"LS-CA-CA9-%"``).
    Pass it directly to sql parameters — do NOT run it through any LIKE-escape
    helper first, because the ``%`` wildcards are intentional.  The database
    helpers DB.barcode_prefix_search() and DB.get_document_by_barcode() already
    handle this correctly.  When calling sqlite3 directly, pass the pattern as-is:

        conn.execute("SELECT ... WHERE barcode LIKE ?", (pattern,))
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Segment vocabularies
# ---------------------------------------------------------------------------

# Case-type codes
_CT_SUPREME = "SC"
_CT_APPEALS = "CA"
_CT_DISTRICT = "DC"
_CT_STATE = "ST"
_CT_STATUTE = "SB"
_CT_BRIEF = "BR"
_CT_OTHER = "OT"

# Subject-matter codes
_SM_CONSTITUTIONAL = "CON"
_SM_CRIMINAL = "CRM"
_SM_CIVIL_RIGHTS = "CIV"
_SM_CONTRACTS = "CTR"
_SM_TORTS = "TRT"
_SM_PROCEDURE = "PRO"
_SM_FAMILY = "FAM"
_SM_IMMIGRATION = "IMM"
_SM_BANKRUPTCY = "BNK"
_SM_PROPERTY = "PRP"
_SM_LABOR = "LAB"
_SM_TAX = "TAX"
_SM_OTHER = "OTH"

# ---------------------------------------------------------------------------
# Jurisdiction lookup tables
# ---------------------------------------------------------------------------

# Maps recognisable strings → 2-4 char JR code (checked in order; first match wins)
_JR_PATTERNS: list[tuple[str, str]] = [
    # Federal circuits (checked before plain "circuit" to be more specific)
    ("first circuit",   "CA1"),
    ("second circuit",  "CA2"),
    ("third circuit",   "CA3"),
    ("fourth circuit",  "CA4"),
    ("fifth circuit",   "CA5"),
    ("sixth circuit",   "CA6"),
    ("seventh circuit", "CA7"),
    ("eighth circuit",  "CA8"),
    ("ninth circuit",   "CA9"),
    ("tenth circuit",   "CA10"),
    ("eleventh circuit","CA11"),
    ("d.c. circuit",    "CAD"),
    ("federal circuit", "CAF"),
    # Catch-all federal appellate
    ("court of appeals","CA"),
    # US Supreme Court
    ("supreme court of the united states", "US"),
    ("u.s. supreme court",                 "US"),
    # Federal district courts — state hints
    ("northern district of california",  "CAN"),
    ("central district of california",   "CAC"),
    ("southern district of california",  "CAS"),
    ("eastern district of california",   "CAE"),
    ("southern district of new york",    "NYS"),
    ("eastern district of new york",     "NYE"),
    ("northern district of new york",    "NYN"),
    ("western district of new york",     "NYW"),
    ("northern district of texas",       "TXN"),
    ("southern district of texas",       "TXS"),
    ("eastern district of texas",        "TXE"),
    ("western district of texas",        "TXW"),
    # Generic district court → use state abbreviation below
    ("district court",                   "DC"),  # placeholder; refined later
    # State supreme courts (rough match)
    ("supreme court of california",  "CAL"),
    ("supreme court of new york",    "NYK"),
    ("supreme court of texas",       "TEX"),
    ("supreme court of florida",     "FLA"),
    ("supreme court of illinois",    "ILL"),
    ("supreme court of ohio",        "OHI"),
    ("supreme court of pennsylvania","PEN"),
]

# USPS two-letter state codes → JR code
_STATE_TO_JR: dict[str, str] = {
    "Alabama": "ALA", "Alaska": "AKA", "Arizona": "ARZ", "Arkansas": "ARK",
    "California": "CAL", "Colorado": "COL", "Connecticut": "CON",
    "Delaware": "DEL", "Florida": "FLA", "Georgia": "GEO", "Hawaii": "HAW",
    "Idaho": "IDA", "Illinois": "ILL", "Indiana": "IND", "Iowa": "IOW",
    "Kansas": "KAN", "Kentucky": "KEN", "Louisiana": "LOU", "Maine": "MAI",
    "Maryland": "MAR", "Massachusetts": "MAS", "Michigan": "MIC",
    "Minnesota": "MIN", "Mississippi": "MIS", "Missouri": "MOI",
    "Montana": "MON", "Nebraska": "NEB", "Nevada": "NEV",
    "New Hampshire": "NHM", "New Jersey": "NJR", "New Mexico": "NMX",
    "New York": "NYK", "North Carolina": "NCA", "North Dakota": "NDA",
    "Ohio": "OHI", "Oklahoma": "OKL", "Oregon": "ORE",
    "Pennsylvania": "PEN", "Rhode Island": "RHO", "South Carolina": "SCA",
    "South Dakota": "SDA", "Tennessee": "TEN", "Texas": "TEX",
    "Utah": "UTA", "Vermont": "VER", "Virginia": "VIR",
    "Washington": "WAS", "West Virginia": "WVI", "Wisconsin": "WIS",
    "Wyoming": "WYO", "District of Columbia": "DC",
}

# Subject-matter keyword mapping: word → SM code (longest-match wins)
_SM_KEYWORDS: list[tuple[str, str]] = [
    ("constitutional",   _SM_CONSTITUTIONAL),
    ("amendment",        _SM_CONSTITUTIONAL),
    ("due process",      _SM_CONSTITUTIONAL),
    ("equal protection", _SM_CONSTITUTIONAL),
    ("first amendment",  _SM_CONSTITUTIONAL),
    ("fourth amendment", _SM_CONSTITUTIONAL),
    ("civil rights",     _SM_CIVIL_RIGHTS),
    ("section 1983",     _SM_CIVIL_RIGHTS),
    ("42 u.s.c",         _SM_CIVIL_RIGHTS),
    ("discrimination",   _SM_CIVIL_RIGHTS),
    ("habeas",           _SM_CRIMINAL),
    ("criminal",         _SM_CRIMINAL),
    ("murder",           _SM_CRIMINAL),
    ("felony",           _SM_CRIMINAL),
    ("sentence",         _SM_CRIMINAL),
    ("guilty",           _SM_CRIMINAL),
    ("indictment",       _SM_CRIMINAL),
    ("probation",        _SM_CRIMINAL),
    ("contract",         _SM_CONTRACTS),
    ("breach",           _SM_CONTRACTS),
    ("warranty",         _SM_CONTRACTS),
    ("tort",             _SM_TORTS),
    ("negligence",       _SM_TORTS),
    ("damages",          _SM_TORTS),
    ("personal injury",  _SM_TORTS),
    ("defamation",       _SM_TORTS),
    ("summary judgment", _SM_PROCEDURE),
    ("jurisdiction",     _SM_PROCEDURE),
    ("standing",         _SM_PROCEDURE),
    ("class action",     _SM_PROCEDURE),
    ("removal",          _SM_PROCEDURE),
    ("divorce",          _SM_FAMILY),
    ("custody",          _SM_FAMILY),
    ("child support",    _SM_FAMILY),
    ("adoption",         _SM_FAMILY),
    ("immigration",      _SM_IMMIGRATION),
    ("deportation",      _SM_IMMIGRATION),
    ("asylum",           _SM_IMMIGRATION),
    ("visa",             _SM_IMMIGRATION),
    ("bankruptcy",       _SM_BANKRUPTCY),
    ("chapter 7",        _SM_BANKRUPTCY),
    ("chapter 11",       _SM_BANKRUPTCY),
    ("chapter 13",       _SM_BANKRUPTCY),
    ("property",         _SM_PROPERTY),
    ("easement",         _SM_PROPERTY),
    ("foreclosure",      _SM_PROPERTY),
    ("labor",            _SM_LABOR),
    ("employment",       _SM_LABOR),
    ("nlra",             _SM_LABOR),
    ("workers comp",     _SM_LABOR),
    ("title vii",        _SM_LABOR),
    ("tax",              _SM_TAX),
    ("irs",              _SM_TAX),
    ("revenue",          _SM_TAX),
]

# ---------------------------------------------------------------------------
# Rule-based segment extractors
# ---------------------------------------------------------------------------

def _extract_ct(text_lower: str, virtual_folder: str) -> str:
    """Determine case type from text and virtual folder."""
    combined = (text_lower[:3000] + " " + (virtual_folder or "")).lower()
    if re.search(r"\b(statute|u\.s\.c\.|c\.f\.r\.|public law|act of congress)\b", combined):
        return _CT_STATUTE
    if re.search(r"\b(brief|motion|petition|complaint|answer|reply)\b", combined):
        return _CT_BRIEF
    if re.search(r"\b(supreme court of the united states|u\.s\. supreme)\b", combined):
        return _CT_SUPREME
    if re.search(r"\b(court of appeals|circuit court|circuit)\b", combined):
        return _CT_APPEALS
    if re.search(r"\b(district court|u\.s\.d\.c\.)\b", combined):
        return _CT_DISTRICT
    if re.search(r"\b(superior court|state court|court of common pleas|appellate division)\b", combined):
        return _CT_STATE
    return _CT_OTHER


def _extract_jr(text_lower: str, entities: dict) -> str:
    """Determine jurisdiction code using pattern table then state fallback."""
    # Try ordered pattern table first
    for pattern, code in _JR_PATTERNS:
        if pattern in text_lower[:4000]:
            # If we matched "district court" generically, refine with state
            if code == "DC":
                for state, jcode in _STATE_TO_JR.items():
                    if state.lower() in text_lower[:4000]:
                        return jcode
                return "FDC"  # federal district, state unknown
            return code

    # Fall back to JURISDICTION entities extracted by tagger.py
    jurisdictions: list[str] = entities.get("JURISDICTION", [])
    for jr_str in jurisdictions:
        jr_lower = jr_str.lower()
        for state, jcode in _STATE_TO_JR.items():
            if state.lower() in jr_lower:
                return jcode
        for pattern, code in _JR_PATTERNS:
            if pattern in jr_lower:
                return code if code != "DC" else "FDC"

    return "UNK"


def _extract_sm(text_lower: str, keywords: list[str]) -> str:
    """Determine subject-matter code using keyword gazetteer.

    Longer keyword phrases are checked before shorter ones so more-specific
    matches (e.g. 'civil rights') beat the generic sub-words they contain.
    """
    haystack = text_lower[:5000] + " " + " ".join(keywords).lower()
    # Sort by pattern length descending so longer phrases match first
    for kw, sm_code in sorted(_SM_KEYWORDS, key=lambda x: len(x[0]), reverse=True):
        if kw in haystack:
            return sm_code
    return _SM_OTHER


def _extract_yr(entities: dict, text_lower: str) -> str:
    """Extract 4-digit decision year."""
    # Try DATE entities first (already parsed by tagger)
    for date_str in entities.get("DATE", []):
        m = re.search(r"(19|20)\d{2}", date_str)
        if m:
            return m.group()
    # Fall back to any 4-digit year in the first 2 KB of text
    m = re.search(r"\b(19[6-9]\d|20[0-2]\d)\b", text_lower[:2000])
    if m:
        return m.group()
    return "0000"


def _seq_from_ref_no(ref_no: str) -> str:
    """Convert 'LC-000042' → '000042'. Pass-through any other string."""
    if ref_no and ref_no.upper().startswith("LC-"):
        return ref_no[3:].zfill(6)[:6]
    # ref_no might already be a bare number
    digits = re.sub(r"\D", "", ref_no or "")
    return digits.zfill(6)[:6] if digits else "000000"


# ---------------------------------------------------------------------------
# Rule-based barcode assembly
# ---------------------------------------------------------------------------

def _build_barcode_rules(
    text: str,
    entities: dict,
    keywords: list[str],
    ref_no: str,
    virtual_folder: str = "",
) -> tuple[str, str]:
    """Return (barcode, 'rules')."""
    tl = text.lower()
    ct = _extract_ct(tl, virtual_folder)
    jr = _extract_jr(tl, entities)
    sm = _extract_sm(tl, keywords)
    yr = _extract_yr(entities, tl)
    sq = _seq_from_ref_no(ref_no)
    barcode = f"LS-{ct}-{jr}-{sm}-{yr}-{sq}"
    return barcode, "rules"


# ---------------------------------------------------------------------------
# LLM-based classification
# ---------------------------------------------------------------------------

_LLM_SYSTEM = (
    "You are a legal document classifier. "
    "Given a short excerpt from a legal document, return ONLY a JSON object with these exact keys: "
    "\"ct\" (case type code), \"jr\" (jurisdiction code), \"sm\" (subject matter code), \"yr\" (4-digit year). "
    "\n\nValid ct values: SC (U.S. Supreme Court), CA (federal circuit court of appeals), "
    "DC (federal district court), ST (state court), SB (statute/regulation), BR (brief/motion), OT (other). "
    "\nValid sm values: CON (constitutional), CRM (criminal), CIV (civil rights), CTR (contracts), "
    "TRT (torts), PRO (procedure), FAM (family), IMM (immigration), BNK (bankruptcy), "
    "PRP (property), LAB (labor/employment), TAX (tax), OTH (other). "
    "\nFor jr: use US for federal/SCOTUS, CA1-CA11 for numbered circuits, CAD for D.C. Circuit, "
    "CAF for Federal Circuit, or a 3-letter state abbreviation (e.g. CAL, TEX, NYK, FLA). "
    "Use UNK if the jurisdiction cannot be determined. "
    "\nFor yr: a 4-digit year string, or '0000' if unknown. "
    "\nRespond with ONLY the JSON object. No explanation, no markdown, no extra text."
)


def _call_llm_classify(text: str, cfg: dict) -> dict[str, str] | None:
    """Ask the configured LLM to classify the document.

    Returns a dict with keys ct, jr, sm, yr, or None on any failure.
    """
    barcode_cfg = cfg.get("barcode", {})
    llm_cfg = cfg.get("llm", {})

    base_url = (
        barcode_cfg.get("llm_base_url")
        or llm_cfg.get("base_url", "https://api.openai.com/v1")
    ).rstrip("/")
    model = (
        barcode_cfg.get("llm_model")
        or llm_cfg.get("fast_model")
        or llm_cfg.get("model", "gpt-4o-mini")
    )
    api_key = os.environ.get("LLM_API_KEY", "") or llm_cfg.get("api_key", "")
    timeout = int(barcode_cfg.get("llm_timeout_seconds", 20))

    # Send first 800 chars — enough for caption, header, and opening paragraphs
    excerpt = text[:800].strip()
    if not excerpt:
        return None

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": f"DOCUMENT EXCERPT:\n\n{excerpt}"},
        ],
        "temperature": 0.0,
        "max_tokens": 80,
    }).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    try:
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        raw = data["choices"][0]["message"]["content"].strip()
        # Strip optional markdown fences
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw).rstrip("`").strip()
        result = json.loads(raw)
        # Validate presence of expected keys
        if all(k in result for k in ("ct", "jr", "sm", "yr")):
            return result
        log.warning("[BARCODE] LLM returned unexpected structure: %s", result)
        return None
    except Exception as exc:
        log.debug("[BARCODE] LLM classify failed: %s", exc)
        return None


def _build_barcode_llm(
    text: str,
    entities: dict,
    keywords: list[str],
    ref_no: str,
    virtual_folder: str,
    cfg: dict,
) -> tuple[str, str]:
    """Return (barcode, 'llm'). Raises RuntimeError if LLM call fails."""
    result = _call_llm_classify(text, cfg)
    if result is None:
        raise RuntimeError("LLM classification returned no result")
    ct = str(result.get("ct", _CT_OTHER)).upper()[:4]
    jr = str(result.get("jr", "UNK")).upper()[:4]
    sm = str(result.get("sm", _SM_OTHER)).upper()[:3]
    yr_raw = str(result.get("yr", "0000"))
    yr = yr_raw if re.fullmatch(r"(19|20)\d{2}", yr_raw) else "0000"
    sq = _seq_from_ref_no(ref_no)
    barcode = f"LS-{ct}-{jr}-{sm}-{yr}-{sq}"
    return barcode, "llm"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assign_barcode(
    text: str,
    entities: dict,
    citations: list[str],
    keywords: list[str],
    ref_no: str,
    cfg: dict | None = None,
    virtual_folder: str = "",
) -> tuple[str, str]:
    """Generate the structured barcode ID for a document.

    Args:
        text:           Full extracted document text.
        entities:       Entity dict produced by tagger.extract_entities().
        citations:      Citation list (unused by rules but available for future use).
        keywords:       Keyword list from tagger.
        ref_no:         The LC-XXXXXX reference number already assigned to this doc.
        cfg:            Full config dict (from config.load_config()).  May be None –
                        falls back to rule engine.
        virtual_folder: The virtual folder string (used as auxiliary signal for CT).

    Returns:
        (barcode, strategy) where strategy is "rules" or "llm".
        e.g. ("LS-CA-CA9-CIV-2019-000042", "llm")
    """
    cfg = cfg or {}
    barcode_cfg = cfg.get("barcode", {})
    strategy = str(barcode_cfg.get("strategy", "llm_with_fallback")).lower()

    if strategy == "rules":
        return _build_barcode_rules(text, entities, keywords, ref_no, virtual_folder)

    if strategy == "llm":
        return _build_barcode_llm(text, entities, keywords, ref_no, virtual_folder, cfg)

    # "llm_with_fallback" (default)
    try:
        return _build_barcode_llm(text, entities, keywords, ref_no, virtual_folder, cfg)
    except Exception as exc:
        log.debug("[BARCODE] LLM strategy failed (%s); using rule fallback.", exc)
        return _build_barcode_rules(text, entities, keywords, ref_no, virtual_folder)


def barcode_prefix(
    ct: str | None = None,
    jr: str | None = None,
    sm: str | None = None,
    yr: str | None = None,
) -> str:
    """Build a raw SQL LIKE pattern for fast prefix filtering.

    Pass only the segments you want to constrain; omit the rest.
    The returned string contains ``%`` wildcard characters and must be
    passed directly to a SQL ``LIKE`` parameter — **do not** run it through
    any LIKE-escape helper (doing so would turn the ``%`` wildcards into
    literals, matching nothing).

    Examples:
        barcode_prefix(jr="CA9")             → "LS-%-CA9-%-%-%" 
        barcode_prefix(ct="CA", jr="CA9")    → "LS-CA-CA9-%-%-%" 
        barcode_prefix(sm="CON")             → "LS-%-%-CON-%-%" 
        barcode_prefix(ct="SC", sm="CON")    → "LS-SC-%-CON-%-%" 

    Usage with sqlite3:
        pattern = barcode_prefix(ct="CA", jr="CA9")
        conn.execute("SELECT ... WHERE barcode LIKE ?", (pattern,))
    """
    ct_part = ct.upper() if ct else "%"
    jr_part = jr.upper() if jr else "%"
    sm_part = sm.upper() if sm else "%"
    yr_part = str(yr) if yr else "%"
    return f"LS-{ct_part}-{jr_part}-{sm_part}-{yr_part}-%"


def parse_barcode(barcode: str) -> dict[str, str]:
    """Decompose a barcode string back into its named segments.

    Returns a dict with keys: namespace, ct, jr, sm, yr, sq.
    Returns an empty dict if the string is not a valid barcode.
    """
    parts = (barcode or "").split("-", 5)
    if len(parts) != 6 or parts[0] != "LS":
        return {}
    return {
        "namespace": parts[0],
        "ct": parts[1],
        "jr": parts[2],
        "sm": parts[3],
        "yr": parts[4],
        "sq": parts[5],
    }
