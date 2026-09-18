from __future__ import annotations

import re
from .dictionary import DictionaryLookup
from .models import Meaning, SenseResult
from .tokenizer import Token, tokenize


def _rule_meaning(token: Token) -> Meaning | None:
    if token.kind != "group":
        return None
    if re.fullmatch(r"\$\s?\d[\d,]*(?:\.\d{2})?", token.text):
        return Meaning("a monetary amount", "rules")
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", token.text) or re.search(r"\b\d{4}\b", token.text):
        return Meaning("a calendar date", "rules")
    if re.match(r"(?:Mr|Mrs|Ms|Dr|Judge|Justice)\.", token.text):
        return Meaning("a person's honorific and name", "rules")
    return Meaning("a street address", "rules")


class WordSenseEngine:
    """Phase 1 analyzer: tokenize, enumerate meanings, and choose one."""

    def __init__(self, dictionary: DictionaryLookup | None = None) -> None:
        self.dictionary = dictionary or DictionaryLookup()

    def analyze(self, document: str) -> list[SenseResult]:
        results: list[SenseResult] = []
        for token in tokenize(document):
            rule_meaning = _rule_meaning(token)
            dictionary_meanings = self.dictionary.meanings(token.text)
            possible = (rule_meaning,) + dictionary_meanings if rule_meaning else dictionary_meanings
            possible = tuple(dict.fromkeys(possible))
            if rule_meaning:
                chosen, confidence, source = rule_meaning, 0.99, "rules"
            elif possible:
                chosen = possible[0]
                source = chosen.source
                confidence = {"legal_dictionary": 0.98, "wordnet": 0.55, "fallback": 0.25}.get(source, 0.2)
                if len(possible) == 1:
                    confidence = min(0.85, confidence + 0.15)
            else:
                chosen, confidence, source = None, 0.0, "none"
            results.append(SenseResult(token.text, token.start, token.end, token.kind, possible, chosen, confidence, source))
        return results
