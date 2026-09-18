"""Phase 1 dictionary and word-sense engine."""

from .models import SenseResult, Meaning, Span
from .sense_engine import WordSenseEngine

__all__ = ["Meaning", "SenseResult", "Span", "WordSenseEngine"]
