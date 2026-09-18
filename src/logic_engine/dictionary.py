from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

LEGAL_TERMS: dict[str, tuple[str, ...]] = {
    "plaintiff": ("the party who brings a civil action",),
    "defendant": ("the party against whom a civil action is brought",),
    "overruled": ("a court rejected or set aside a prior ruling or objection",),
    "affirmed": ("an appellate court upheld the lower court's decision",),
    "reversed": ("an appellate court set aside the lower court's decision",),
    "dismissed": ("an action or claim was terminated without the requested relief",),
    "holding": ("the court's legal determination necessary to decide the case",),
    "docket": ("the court's record and chronological list of proceedings",),
}

# Used only when WordNet is unavailable (for example, a fresh checkout before
# the optional NLTK corpus is downloaded). It keeps the demo useful offline.
_FALLBACK: dict[str, tuple[str, ...]] = {
    "court": ("an institution where legal cases are heard", "a courtyard or enclosed area"),
    "case": ("a legal action or proceeding", "a particular instance or example"),
    "order": ("a direction issued by a court", "a state of organized arrangement"),
    "appeal": ("a request for review by a higher court", "an earnest request"),
    "file": ("to submit a document to a court", "a collection of records", "a tool for smoothing material"),
    "right": ("a legal entitlement", "correct or proper", "the opposite of left"),
}


@dataclass
class DictionaryLookup:
    """WordNet-backed lookup with a small deterministic offline fallback."""

    def __post_init__(self) -> None:
        self._wordnet = None
        try:
            from nltk.corpus import wordnet
            # Accessing synsets here detects a missing downloaded corpus.
            wordnet.synsets("example")
            self._wordnet = wordnet
        except (ImportError, LookupError):
            self._wordnet = None

    @lru_cache(maxsize=4096)
    def meanings(self, text: str) -> tuple[Meaning, ...]:
        key = text.casefold().strip()
        if key in LEGAL_TERMS:
            return tuple(Meaning(definition, "legal_dictionary") for definition in LEGAL_TERMS[key])
        if self._wordnet is not None and " " not in key:
            meanings: list[Meaning] = []
            for synset in self._wordnet.synsets(key):
                meaning = Meaning(synset.definition(), "wordnet", synset.pos())
                if meaning not in meanings:
                    meanings.append(meaning)
            if meanings:
                return tuple(meanings)
        return tuple(Meaning(definition, "fallback") for definition in _FALLBACK.get(key, ()))
