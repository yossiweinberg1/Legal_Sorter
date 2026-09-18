from __future__ import annotations

import re
from dataclasses import dataclass

from .tokenizer import Token

ALWAYS_TRIGGERS = frozenset(
    "stole filed ordered denied granted appealed reversed affirmed overruled remanded "
    "dismissed settled paid convicted sentenced plaintiff defendant petitioner respondent".split()
)
SOFT_TRIGGERS = frozenset("because due after before while when thereafter".split())
_TITLE_RE = re.compile(r"^(?:Mr|Ms|Mrs|Judge|Justice)\.", re.I)
_COURT_RE = re.compile(r"\bCourt\s+of\s+(?:Appeals|Appeal|the\s+United\s+States|[A-Z][\w.-]*)", re.I)


@dataclass(frozen=True)
class Trigger:
    text: str
    start: int
    end: int
    kind: str
    soft: bool = False


def find_triggers(document: str, tokens: list[Token]) -> list[Trigger]:
    triggers: list[Trigger] = []
    for token in tokens:
        lower = token.text.casefold()
        if lower in ALWAYS_TRIGGERS:
            triggers.append(Trigger(token.text, token.start, token.end, "word"))
        elif lower in SOFT_TRIGGERS:
            triggers.append(Trigger(token.text, token.start, token.end, "soft", True))
        elif token.kind == "group" and token.text.startswith(("Mr.", "Ms.", "Mrs.", "Judge.", "Justice.")):
            triggers.append(Trigger(token.text, token.start, token.end, "title_name"))
        elif token.kind == "group" and token.text.startswith("$"):
            triggers.append(Trigger(token.text, token.start, token.end, "money"))
        elif token.kind == "group" and (re.search(r"\d", token.text) and token.text[0].isdigit()):
            triggers.append(Trigger(token.text, token.start, token.end, "date_or_address"))

    for match in _COURT_RE.finditer(document):
        triggers.append(Trigger(match.group(), match.start(), match.end(), "court"))
    return sorted(triggers, key=lambda item: (item.start, item.end))
