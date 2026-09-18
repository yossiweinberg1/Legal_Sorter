from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SLOT_NAMES = ("who", "what", "where", "when", "how", "why", "outcome")
HypothesisStatus = Literal["=", "≠"]
FrameStatus = Literal["open", "partially_filled", "closed"]


@dataclass
class Hypothesis:
    hypothesis_id: str
    frame_id: str
    slot: str
    value: str
    start: int
    end: int
    confidence: float
    source: Literal["direct", "contextual", "rules"]
    status: HypothesisStatus = "="
    rank: int = 1

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Frame:
    frame_id: str
    doc_id: str
    trigger_text: str
    trigger_start: int
    trigger_end: int
    parent_frame_id: str | None = None
    status: FrameStatus = "open"
    best_values: dict[str, str | None] = field(
        default_factory=lambda: {slot: None for slot in SLOT_NAMES}
    )

    def to_dict(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "doc_id": self.doc_id,
            "trigger_text": self.trigger_text,
            "trigger_start": self.trigger_start,
            "trigger_end": self.trigger_end,
            "parent_frame_id": self.parent_frame_id,
            "status": self.status,
            "best_values": self.best_values.copy(),
        }
