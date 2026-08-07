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


def _metrics(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 0.0 if precision + recall == 0 else (2 * precision * recall / (precision + recall))
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "counts": {"tp": tp, "fp": fp, "fn": fn},
    }


def _load_jsonl(path: str) -> list[dict]:
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def evaluate_records(records: list[dict]) -> dict:
    totals = {
        "citation": {"tp": 0, "fp": 0, "fn": 0},
        "entity": {"tp": 0, "fp": 0, "fn": 0},
    }
    slices: dict[str, dict] = {}
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

        ctp, cfp, cfn = _pr_counts(pred_citations, gold_citations)
        etp, efp, efn = _pr_counts(pred_entities, gold_entities)
        for bucket, tp, fp, fn in [
            ("citation", ctp, cfp, cfn),
            ("entity", etp, efp, efn),
        ]:
            totals[bucket]["tp"] += tp
            totals[bucket]["fp"] += fp
            totals[bucket]["fn"] += fn

        slice_keys = {
            "document_type": rec.get("document_type"),
            "jurisdiction": rec.get("jurisdiction"),
            "noise_profile": rec.get("noise_profile"),
        }
        for key, value in slice_keys.items():
            if not value:
                continue
            label = f"{key}:{value}"
            bucket = slices.setdefault(
                label,
                {"cases_evaluated": 0, "citation": {"tp": 0, "fp": 0, "fn": 0}, "entity": {"tp": 0, "fp": 0, "fn": 0}},
            )
            bucket["cases_evaluated"] += 1
            bucket["citation"]["tp"] += ctp
            bucket["citation"]["fp"] += cfp
            bucket["citation"]["fn"] += cfn
            bucket["entity"]["tp"] += etp
            bucket["entity"]["fp"] += efp
            bucket["entity"]["fn"] += efn

    citation_metrics = _metrics(**totals["citation"])
    entity_metrics = _metrics(**totals["entity"])
    report = {
        "cases_evaluated": valid_cases,
        "citation_precision": citation_metrics["precision"],
        "citation_recall": citation_metrics["recall"],
        "citation_f1": citation_metrics["f1"],
        "entity_precision": entity_metrics["precision"],
        "entity_recall": entity_metrics["recall"],
        "entity_f1": entity_metrics["f1"],
        "citation_counts": citation_metrics["counts"],
        "entity_counts": entity_metrics["counts"],
        "slices": {},
    }
    for label, bucket in sorted(slices.items()):
        citation = _metrics(**bucket["citation"])
        entity = _metrics(**bucket["entity"])
        report["slices"][label] = {
            "cases_evaluated": bucket["cases_evaluated"],
            "citation_f1": citation["f1"],
            "entity_f1": entity["f1"],
        }
    return report


def evaluate_jsonl(path: str) -> dict:
    return evaluate_records(_load_jsonl(path))


def load_baseline(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compare_to_baseline(report: dict, baseline: dict) -> list[str]:
    failures = []
    for metric in ("citation_f1", "entity_f1", "citation_precision", "citation_recall"):
        if metric not in baseline:
            continue
        actual = float(report.get(metric, 0.0))
        expected = float(baseline[metric])
        if actual + 1e-9 < expected:
            failures.append(f"{metric} {actual:.3f} < baseline {expected:.3f}")
    return failures


def check_quality_gate(
    report: dict,
    *,
    citation_f1_min: float,
    entity_f1_min: float,
    min_cases: int,
    baseline: dict | None = None,
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
    if baseline:
        failures.extend(compare_to_baseline(report, baseline))
    return len(failures) == 0, failures
