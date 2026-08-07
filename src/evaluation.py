"""Benchmark evaluation for deterministic tagging/citation quality."""

from __future__ import annotations

import json
from pathlib import Path

from . import tagger


def _safe_set(items) -> set[str]:
    if not items:
        return set()
    return {str(i).strip() for i in items if str(i).strip()}


def _flatten_entities(entities: dict) -> set[str]:
    if not isinstance(entities, dict):
        return set()
    flat: set[str] = set()
    for values in entities.values():
        if isinstance(values, list):
            flat.update(_safe_set(values))
        elif values:
            flat.add(str(values).strip())
    return {x for x in flat if x}


def _pr_counts(pred: set[str], gold: set[str]) -> tuple[int, int, int]:
    tp = len(pred & gold)
    fp = len(pred - gold)
    fn = len(gold - pred)
    return tp, fp, fn


def _f1(tp: int, fp: int, fn: int) -> float:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _load_jsonl(path: str) -> list[dict]:
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def evaluate_records(records: list[dict]) -> dict:
    citation_tp = citation_fp = citation_fn = 0
    entity_tp = entity_fp = entity_fn = 0
    valid_cases = 0

    for rec in records:
        text = str(rec.get("text", "")).strip()
        if not text:
            continue
        valid_cases += 1

        gold_citations = _safe_set(rec.get("expected_citations", []))
        gold_entities = _safe_set(rec.get("expected_entities", []))

        pred_citations = _safe_set(tagger.extract_citations(text))
        pred_entities = _flatten_entities(tagger.extract_entities(text))

        tp, fp, fn = _pr_counts(pred_citations, gold_citations)
        citation_tp += tp
        citation_fp += fp
        citation_fn += fn

        tp, fp, fn = _pr_counts(pred_entities, gold_entities)
        entity_tp += tp
        entity_fp += fp
        entity_fn += fn

    return {
        "cases_evaluated": valid_cases,
        "citation_f1": _f1(citation_tp, citation_fp, citation_fn),
        "entity_f1": _f1(entity_tp, entity_fp, entity_fn),
        "citation_counts": {"tp": citation_tp, "fp": citation_fp, "fn": citation_fn},
        "entity_counts": {"tp": entity_tp, "fp": entity_fp, "fn": entity_fn},
    }


def evaluate_jsonl(path: str) -> dict:
    return evaluate_records(_load_jsonl(path))


def check_quality_gate(
    report: dict, *, citation_f1_min: float, entity_f1_min: float, min_cases: int
) -> tuple[bool, list[str]]:
    failures = []
    if report.get("cases_evaluated", 0) < int(min_cases):
        failures.append(
            f"cases_evaluated {report.get('cases_evaluated', 0)} < required {int(min_cases)}"
        )
    if float(report.get("citation_f1", 0.0)) < float(citation_f1_min):
        failures.append(
            f"citation_f1 {report.get('citation_f1', 0.0):.3f} < min {float(citation_f1_min):.3f}"
        )
    if float(report.get("entity_f1", 0.0)) < float(entity_f1_min):
        failures.append(
            f"entity_f1 {report.get('entity_f1', 0.0):.3f} < min {float(entity_f1_min):.3f}"
        )
    return len(failures) == 0, failures

