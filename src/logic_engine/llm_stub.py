"""Reserved interface for a future one-word-at-a-time sense selector."""

from __future__ import annotations

from typing import Sequence

from .models import Meaning


class SmallLLMSenseSelector:
    """Phase 1 stub. It intentionally makes no model or network calls."""

    def choose(self, word: str, context: Sequence[str], meanings: Sequence[Meaning]) -> Meaning | None:
        raise NotImplementedError("LLM sense selection is reserved for a later phase")
