import unittest

from src.evaluation import evaluate_records, check_quality_gate


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

    def test_quality_gate_fails_below_threshold(self):
        report = {"cases_evaluated": 1, "citation_f1": 0.2, "entity_f1": 0.1}
        ok, failures = check_quality_gate(report, citation_f1_min=0.7, entity_f1_min=0.6, min_cases=1)
        self.assertFalse(ok)
        self.assertGreaterEqual(len(failures), 2)


if __name__ == "__main__":
    unittest.main()

