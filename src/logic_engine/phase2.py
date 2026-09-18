from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import count

from .frame_models import Frame, Hypothesis, SLOT_NAMES
from .sense_engine import WordSenseEngine
from .tokenizer import Token, tokenize
from .triggers import Trigger, find_triggers

_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]?", re.S)
_OUTCOME = frozenset("affirmed reversed remanded dismissed granted denied convicted sentenced settled paid".split())
_ROLE = frozenset("plaintiff defendant petitioner respondent".split())
_PREPOSITIONS = frozenset("by through using with via".split())


@dataclass
class Phase2Result:
    doc_id: str
    frames: list[Frame]
    hypotheses: list[Hypothesis]
    sense_results: list

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "frames": [frame.to_dict() for frame in self.frames],
            "hypotheses": [hypothesis.to_dict() for hypothesis in self.hypotheses],
        }


class FrameEngine:
    """Build Phase 2 frames from the existing Phase 1 sense-tagged stream."""

    def __init__(self, sense_engine: WordSenseEngine | None = None) -> None:
        self.sense_engine = sense_engine or WordSenseEngine()
        self._ids = count(1)

    def analyze(self, doc_id: str, text: str) -> Phase2Result:
        senses = self.sense_engine.analyze(text)
        tokens = tokenize(text)
        triggers = find_triggers(text, tokens)
        sentences = list(_SENTENCE_RE.finditer(text)) or [re.match(r".*", text)]
        frames: list[Frame] = []
        hypotheses: list[Hypothesis] = []

        for trigger in triggers:
            parent = self._parent_for_trigger(frames, trigger)
            if trigger.soft and parent is not None:
                self._fill_frame(parent, trigger, text, tokens, hypotheses, sentences)
                continue
            depth = self._depth(parent, frames)
            if depth >= 2:
                # Children cannot create grandchildren; soft context can still
                # improve an existing child below.
                continue
            frame = Frame(
                frame_id=f"{doc_id}:frame-{next(self._ids)}",
                doc_id=doc_id,
                trigger_text=trigger.text,
                trigger_start=trigger.start,
                trigger_end=trigger.end,
                parent_frame_id=parent.frame_id if parent else None,
            )
            frames.append(frame)
            self._fill_frame(frame, trigger, text, tokens, hypotheses, sentences)

        # Raised frames receive a second pass over their nearby and later text.
        for frame in frames:
            self._fill_frame(frame, None, text, tokens, hypotheses, sentences)
            filled = sum(value is not None for value in frame.best_values.values())
            frame.status = "partially_filled" if filled else "open"
        return Phase2Result(doc_id, frames, hypotheses, senses)

    def _parent_for_trigger(self, frames: list[Frame], trigger: Trigger) -> Frame | None:
        candidates = [frame for frame in frames if frame.trigger_start < trigger.start]
        return candidates[-1] if candidates else None

    @staticmethod
    def _depth(frame: Frame | None, frames: list[Frame]) -> int:
        depth = 0
        while frame is not None:
            depth += 1
            frame = next((f for f in frames if f.frame_id == frame.parent_frame_id), None)
        return depth

    def _fill_frame(self, frame: Frame, trigger: Trigger | None, text: str,
                    tokens: list[Token], hypotheses: list[Hypothesis], sentences: list) -> None:
        anchor = trigger.start if trigger else frame.trigger_start
        sentence = next((s for s in sentences if s.start() <= anchor <= s.end()), None)
        if sentence is None:
            return
        start = max(0, sentence.start() - (sentences[sentences.index(sentence) - 1].start() if sentences.index(sentence) else 0))
        # Current sentence plus the previous sentence, bounded at document start.
        index = sentences.index(sentence)
        context_start = sentences[max(0, index - 1)].start()
        context_end = sentence.end()
        context_tokens = [t for t in tokens if context_start <= t.start < context_end]
        candidates = self._candidates(frame, trigger, context_tokens, text)
        for slot, value, token, confidence, source in candidates:
            self._add_hypothesis(frame, slot, value, token.start, token.end,
                                 confidence, source, hypotheses)

    def _candidates(self, frame: Frame, trigger: Trigger | None, tokens: list[Token], text: str):
        trigger_word = trigger.text.casefold() if trigger else frame.trigger_text.casefold()
        for i, token in enumerate(tokens):
            lower = token.text.casefold().strip(".,;:")
            if token.kind == "group":
                if token.text.startswith("$"):
                    yield "outcome", token.text, token, .92, "direct"
                elif re.search(r"\d", token.text) and ("/" in token.text or re.search(r"\b\d{4}\b", token.text)):
                    yield "when", token.text, token, .95, "direct"
                elif token.text.startswith(("Mr.", "Ms.", "Mrs.", "Judge.", "Justice.")):
                    yield "who", token.text, token, .9, "direct"
                elif re.search(r"\b(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Drive|Dr)\.?$", token.text):
                    yield "where", token.text, token, .9, "direct"
            if lower in _ROLE:
                yield "who", token.text, token, .96, "direct"
            if lower in _OUTCOME:
                yield "outcome", token.text, token, .92, "direct"
            if lower in {"court", "appeals", "judge", "justice"}:
                yield "where", token.text, token, .65, "contextual"
            if lower in {"because", "due", "after", "before", "while", "when", "thereafter"}:
                yield "why", token.text, token, .7, "contextual"
            if lower in _PREPOSITIONS and i + 1 < len(tokens):
                next_token = tokens[i + 1]
                yield "how", text[token.start:next_token.end], token, .62, "contextual"
            if trigger and token.start > trigger.end and token.start - trigger.end < 90 and token.kind == "word":
                if frame.best_values.get("what") is None and lower not in _ROLE:
                    yield "what", token.text, token, .55, "contextual"

    def _add_hypothesis(self, frame: Frame, slot: str, value: str, start: int, end: int,
                        confidence: float, source: str, hypotheses: list[Hypothesis]) -> None:
        if slot not in SLOT_NAMES:
            return
        existing = [h for h in hypotheses if h.frame_id == frame.frame_id and h.slot == slot and h.status == "="]
        if any(h.value == value for h in existing):
            return
        rank = len([h for h in hypotheses if h.frame_id == frame.frame_id and h.slot == slot]) + 1
        if existing and confidence > existing[0].confidence:
            for hypothesis in existing:
                hypothesis.status = "≠"
        hypothesis = Hypothesis(
            hypothesis_id=f"{frame.frame_id}:h-{len(hypotheses) + 1}", frame_id=frame.frame_id,
            slot=slot, value=value, start=start, end=end, confidence=confidence,
            source=source, rank=rank,
        )
        hypotheses.append(hypothesis)
        current = next((h for h in hypotheses if h.frame_id == frame.frame_id and h.slot == slot and h.status == "="), hypothesis)
        frame.best_values[slot] = current.value
