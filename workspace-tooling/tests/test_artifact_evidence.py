from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_evidence as evidence_schema  # noqa: E402


class CheckTests(unittest.TestCase):
    """F-16: evidence must carry its own bar, not just an aggregate verdict."""

    def test_check_records_threshold_observed_operator_and_verdict(self) -> None:
        record = evidence_schema.check(
            "exact_logical_mean_top10_overlap",
            observed=0.9878,
            operator=">=",
            threshold=0.98,
        )
        self.assertEqual(
            record,
            {
                "name": "exact_logical_mean_top10_overlap",
                "observed": 0.9878,
                "operator": ">=",
                "threshold": 0.98,
                "verdict": "pass",
            },
        )

    def test_verdict_is_derived_not_asserted(self) -> None:
        """The whole point: a verdict cannot contradict its own threshold."""
        failing = evidence_schema.check(
            "recall_at_10", observed=0.80, operator=">=", threshold=0.90
        )
        self.assertEqual(failing["verdict"], "fail")
        # There is no caller-supplied verdict argument to disagree with.
        with self.assertRaises(TypeError):
            evidence_schema.check(
                "recall_at_10",
                observed=0.80,
                operator=">=",
                threshold=0.90,
                verdict="pass",
            )

    def test_derived_threshold_records_its_basis(self) -> None:
        record = evidence_schema.check(
            "mrr_at_10_vs_baseline",
            observed=0.91,
            operator=">=",
            threshold=0.98 * 0.90,
            basis={
                "expression": "0.98 * best_simple_baseline.mrr_at_10",
                "factor": 0.98,
                "reference": 0.90,
            },
        )
        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["threshold_basis"]["factor"], 0.98)
        self.assertEqual(record["threshold_basis"]["reference"], 0.90)

    def test_basis_without_expression_is_rejected(self) -> None:
        with self.assertRaisesRegex(evidence_schema.EvidenceError, "expression"):
            evidence_schema.check(
                "x", observed=1.0, operator=">=", threshold=1.0, basis={"factor": 0.5}
            )

    def test_unsupported_operator_is_rejected(self) -> None:
        with self.assertRaisesRegex(evidence_schema.EvidenceError, "unsupported operator"):
            evidence_schema.check("x", observed=1, operator="~=", threshold=1)

    def test_nonfinite_values_are_rejected(self) -> None:
        for bad in (float("nan"), float("inf")):
            with self.assertRaisesRegex(evidence_schema.EvidenceError, "finite"):
                evidence_schema.check("x", observed=bad, operator=">=", threshold=0.0)
            with self.assertRaisesRegex(evidence_schema.EvidenceError, "finite"):
                evidence_schema.check("x", observed=0.0, operator=">=", threshold=bad)

    def test_non_numeric_value_is_rejected(self) -> None:
        with self.assertRaisesRegex(evidence_schema.EvidenceError, "real number"):
            evidence_schema.check("x", observed="0.9", operator=">=", threshold=0.9)


class ObservationTests(unittest.TestCase):
    def test_ungated_value_must_say_why(self) -> None:
        record = evidence_schema.observation(
            "approximate_top1_match_rate",
            observed=0.88,
            reason="approximate search is non-deterministic; not a gate",
        )
        self.assertFalse(record["gated"])
        self.assertIn("non-deterministic", record["reason"])

    def test_missing_reason_is_rejected(self) -> None:
        with self.assertRaisesRegex(evidence_schema.EvidenceError, "why it is ungated"):
            evidence_schema.observation("x", observed=1.0, reason="")


class SummarizeTests(unittest.TestCase):
    def _checks(self, *pairs: tuple[str, float]) -> list[dict]:
        return [
            evidence_schema.check(name, observed=value, operator=">=", threshold=0.5)
            for name, value in pairs
        ]

    def test_all_passing_yields_passed(self) -> None:
        summary = evidence_schema.summarize(self._checks(("a", 0.9), ("b", 0.6)))
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(summary["checks_failed"], [])
        self.assertEqual(summary["checks_total"], 2)

    def test_one_failure_names_the_failing_check(self) -> None:
        summary = evidence_schema.summarize(self._checks(("a", 0.9), ("b", 0.1)))
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["checks_failed"], ["b"])

    def test_empty_gate_is_rejected(self) -> None:
        """A gate with no checks would report 'passed' while proving nothing."""
        with self.assertRaisesRegex(evidence_schema.EvidenceError, "at least one check"):
            evidence_schema.summarize([])

    def test_duplicate_check_names_are_rejected(self) -> None:
        with self.assertRaisesRegex(evidence_schema.EvidenceError, "duplicate check names"):
            evidence_schema.summarize(self._checks(("a", 0.9), ("a", 0.6)))

    def test_legacy_boolean_map_round_trips(self) -> None:
        summary = evidence_schema.summarize(self._checks(("a", 0.9), ("b", 0.1)))
        self.assertEqual(
            evidence_schema.legacy_boolean_map(summary), {"a": True, "b": False}
        )


class GateIntegrationTests(unittest.TestCase):
    """The eval gate keeps its legacy contract while gaining self-description."""

    def test_retrieval_eval_gate_emits_both_shapes(self) -> None:
        import artifact_retrieval_eval as evaluator

        final_metrics = {
            "recall_at_10": 0.95,
            "mrr_at_10": 0.90,
            "ndcg_at_10": 0.90,
            "exact_citation_accuracy": 0.99,
            "hash_verification_rate": 1.0,
            "negative_abstention_accuracy": 0.97,
            "noncurrent_or_historical_slots": 0,
            "historical_target_leaks": 0,
            "hard_negative_slots": 0,
            "duplicate_slots": 0,
            "verification_failures": 0,
        }
        baselines = [{"mrr_at_10": 0.80, "ndcg_at_10": 0.80}]
        gate = evaluator._gate(final_metrics, baselines)

        # Legacy contract the loader and release policy depend on.
        self.assertEqual(gate["status"], "passed")
        self.assertTrue(all(gate["checks"].values()))
        self.assertIsInstance(gate["checks"]["recall_at_10"], bool)

        # New self-describing records.
        by_name = {item["name"]: item for item in gate["check_records"]}
        self.assertEqual(by_name["recall_at_10"]["threshold"], 0.90)
        self.assertEqual(by_name["recall_at_10"]["observed"], 0.95)
        self.assertEqual(by_name["recall_at_10"]["operator"], ">=")
        self.assertEqual(by_name["recall_at_10"]["verdict"], "pass")
        self.assertAlmostEqual(
            by_name["mrr_at_10_vs_baseline"]["threshold"], 0.98 * 0.80
        )
        self.assertEqual(
            by_name["mrr_at_10_vs_baseline"]["threshold_basis"]["reference"], 0.80
        )

    def test_retrieval_eval_gate_failure_names_the_check(self) -> None:
        import artifact_retrieval_eval as evaluator

        final_metrics = {
            "recall_at_10": 0.10,
            "mrr_at_10": 0.90,
            "ndcg_at_10": 0.90,
            "exact_citation_accuracy": 0.99,
            "hash_verification_rate": 1.0,
            "negative_abstention_accuracy": 0.97,
            "noncurrent_or_historical_slots": 0,
            "historical_target_leaks": 0,
            "hard_negative_slots": 0,
            "duplicate_slots": 0,
            "verification_failures": 0,
        }
        gate = evaluator._gate(final_metrics, [{"mrr_at_10": 0.80, "ndcg_at_10": 0.80}])
        self.assertEqual(gate["status"], "failed")
        self.assertEqual(gate["checks_failed"], ["recall_at_10"])
        self.assertFalse(gate["checks"]["recall_at_10"])


if __name__ == "__main__":
    unittest.main()
