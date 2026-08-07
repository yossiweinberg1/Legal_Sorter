import unittest
from pathlib import Path

from src.evaluation import evaluate_records, check_quality_gate, load_baseline


class EvaluationTests(unittest.TestCase):
    def test_evaluate_records_scores_citations_and_entities(self):
        records = [
            {
                "text": "Smith v. Jones, 410 U.S. 113 (1973). Decided on January 1, 2020 in California Supreme Court.",
                "expected_citations": ["410 U.S. 113 (1973)"],
                "expected_entities": ["Smith v. Jones", "January 1, 2020", "California", "Supreme Court"],
            },
            {
                "text": "No legal citation appears here.",
                "expected_citations": [],
                "expected_entities": [],
            },
        ]
        report = evaluate_records(records)
        self.assertEqual(report["cases_evaluated"], 2)
        self.assertGreaterEqual(report["citation_f1"], 0.9)
        self.assertGreaterEqual(report["entity_f1"], 0.5)
        self.assertIn("citation_precision", report)
        self.assertIn("slices", report)

    def test_quality_gate_fails_below_threshold(self):
        report = {"cases_evaluated": 1, "citation_f1": 0.2, "entity_f1": 0.1}
        ok, failures = check_quality_gate(report, citation_f1_min=0.7, entity_f1_min=0.6, min_cases=1)
        self.assertFalse(ok)
        self.assertGreaterEqual(len(failures), 2)

    def test_quality_gate_compares_to_baseline(self):
        baseline = load_baseline(
            str(Path(__file__).resolve().parent / "fixtures" / "gold_baseline.json")
        )
        report = {
            "cases_evaluated": 5,
            "citation_f1": 0.90,
            "entity_f1": 0.60,
            "citation_precision": 0.90,
            "citation_recall": 0.90,
        }
        ok, failures = check_quality_gate(
            report,
            citation_f1_min=0.70,
            entity_f1_min=0.50,
            min_cases=1,
            baseline=baseline,
        )
        self.assertFalse(ok)
        self.assertTrue(any("baseline" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main()
