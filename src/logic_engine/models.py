from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Source = Literal["wordnet", "legal_dictionary", "rules", "fallback", "none"]


@dataclass(frozen=True)
class Span:
    """A half-open character span in the source document."""

    start: int
    end: int


@dataclass(frozen=True)
class Meaning:
    definition: str
    source: Source
    part_of_speech: str | None = None


@dataclass(frozen=True)
class SenseResult:
    text: str
    start: int
    end: int
    kind: Literal["word", "group"]
    possible_meanings: tuple[Meaning, ...]
    chosen_meaning: Meaning | None
    confidence: float
    choice_source: Source

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
            "possible_meanings": [m.__dict__ for m in self.possible_meanings],
            "chosen_meaning": self.chosen_meaning.__dict__ if self.chosen_meaning else None,
            "confidence": self.confidence,
            "choice_source": self.choice_source,
        }
