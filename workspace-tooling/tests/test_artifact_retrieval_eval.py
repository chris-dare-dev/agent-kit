from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
import stat
import sys
import tempfile
import unittest

import platform_skips
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_retrieval_eval as evaluation  # noqa: E402


def digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def judgment(index: int, *, grade: int | None = None) -> dict[str, object]:
    return {
        "relative_path": f"plans/answer-{index:02d}.md",
        "revision_id": f"revision:{digest(f'revision-{index}')}",
        "span_id": f"span:{digest(f'span-{index}')}",
        "point_id": f"00000000-0000-5000-8000-{index:012x}",
        "byte_start": index * 100,
        "byte_end": index * 100 + 80,
        "span_sha256": digest(f"span-bytes-{index}"),
        "grade": grade if grade is not None else (index % 3) + 1,
    }


def positive_record(split: str, index: int) -> dict[str, object]:
    expected = judgment(index)
    return {
        "query_id": f"{split}-p{index:02d}",
        "split": split,
        "query": f"What exact decision is recorded for item {index}?",
        "categories": ["decision", "identifier"],
        "answerable": True,
        "expected_relative_path": expected["relative_path"],
        "expected_revision_id": expected["revision_id"],
        "old_chunk_sha256": digest(f"old-chunk-{index}"),
        "expected_heading": f"Decision {index}",
        "hard_negative_paths": [f"plans/hard-negative-{index:02d}.md"],
        "allowed_alternate_paths": [],
        "scope": "local_owner/current_only",
        "adjudication": "independently_adjudicated",
        "judgments": [expected],
    }


def negative_record(split: str, index: int) -> dict[str, object]:
    return {
        "query_id": f"{split}-n{index:02d}",
        "split": split,
        "query": f"What was approved for absent identifier MIRAGE-{index:02d}?",
        "categories": ["checked_absent", "expected_abstention"],
        "answerable": False,
        "expected_relative_path": None,
        "expected_revision_id": None,
        "old_chunk_sha256": None,
        "expected_heading": None,
        "hard_negative_paths": [],
        "allowed_alternate_paths": [],
        "scope": "local_owner/current_only",
        "adjudication": "independently_adjudicated",
        "judgments": [],
    }


def gold_document(split: str = "dev") -> dict[str, object]:
    return {
        "schema_version": evaluation.SCHEMA_VERSION,
        "gold_version": evaluation.GOLD_VERSION,
        "generation": "g20260718v2",
        "span_manifest_digest": digest("span-manifest"),
        "split_methodology": "stratified-explicit-v1",
        "adjudication": "same-session-not-blinded",
        "split": split,
        "records": [
            *(positive_record(split, index) for index in range(1, 21)),
            *(negative_record(split, index) for index in range(1, 11)),
        ],
    }


def gold_manifest_document(
    *,
    dev_sha256: str | None = None,
    holdout_sha256: str | None = None,
    dev_file: str = "retrieval-gold-v1.dev.json",
    holdout_file: str = "retrieval-gold-v1.holdout.json",
) -> dict[str, object]:
    return {
        "schema_version": evaluation.SCHEMA_VERSION,
        "gold_version": evaluation.GOLD_VERSION,
        "generation": "g20260718v2",
        "span_manifest_digest": digest("span-manifest"),
        "split_methodology": "stratified-explicit-v1",
        "adjudication": "same-session-not-blinded",
        "splits": {
            "dev": {
                "file": dev_file,
                "sha256": dev_sha256 or digest("dev-split-bytes"),
                "record_count": 30,
            },
            "holdout": {
                "file": holdout_file,
                "sha256": holdout_sha256 or digest("holdout-split-bytes"),
                "record_count": 30,
            },
        },
    }


def exact_result(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "relative_path": value["relative_path"],
        "revision_id": value["revision_id"],
        "span_id": value["span_id"],
        "point_id": value["point_id"],
        "content_sha256": digest(f"content-{value['span_id']}"),
        "span_sha256": value["span_sha256"],
        "span": {
            "byte_start": value["byte_start"],
            "byte_end": value["byte_end"],
        },
        "source_sha256_verified": True,
        "span_sha256_verified": True,
        "manifest_current": True,
    }


def retrieval_response(
    results: list[dict[str, object]],
    *,
    abstained: bool = False,
    cross_score: float = 0.8,
    cross_margin: float = 0.3,
) -> dict[str, object]:
    return {
        "abstained": abstained,
        "abstention_reason": "test-policy" if abstained else None,
        "abstention_features": {
            "has_candidate": bool(results),
            "exact_identifier": False,
            "channel_agreement": True,
            "vector_score": 0.7,
            "lexical_bm25": -2.0,
            "cross_score": cross_score,
            "cross_margin": cross_margin,
        },
        "results": results,
    }


class GoldSchemaValidationTests(unittest.TestCase):
    def test_valid_split_is_normalized_content_addressed_and_isolated(self) -> None:
        document = gold_document("dev")
        suite = evaluation.validate_gold(document, expected_split="dev")
        reordered = {
            key: copy.deepcopy(document[key])
            for key in reversed(list(document))
        }
        repeated = evaluation.validate_gold(reordered, expected_split="dev")

        self.assertEqual(len(suite.records), 30)
        self.assertEqual(len(suite.dev), 30)
        self.assertEqual(suite.holdout, ())
        self.assertEqual(suite.gold_digest, repeated.gold_digest)
        self.assertEqual(
            suite.gold_digest,
            hashlib.sha256(evaluation._canonical_json(document)).hexdigest(),
        )
        document["records"][0]["query"] = "mutated after validation"
        self.assertNotEqual(suite.records[0]["query"], "mutated after validation")

    def test_holdout_file_cannot_be_loaded_as_development(self) -> None:
        with self.assertRaisesRegex(evaluation.EvalError, "not expected"):
            evaluation.validate_gold(
                gold_document("holdout"),
                expected_split="dev",
            )

    def test_every_record_must_stay_inside_the_wrapper_split(self) -> None:
        document = gold_document("dev")
        document["records"][0]["split"] = "holdout"
        with self.assertRaisesRegex(evaluation.EvalError, "split boundary"):
            evaluation.validate_gold(document, expected_split="dev")

    def test_fixed_counts_class_balance_and_unique_ids_are_enforced(self) -> None:
        cases: list[tuple[str, object, str]] = []

        wrong_count = gold_document()
        wrong_count["records"].pop()
        cases.append(("record count", wrong_count, "exactly 30"))

        wrong_balance = gold_document()
        wrong_balance["records"][0] = negative_record("dev", 99)
        cases.append(("class balance", wrong_balance, "20 answerable"))

        duplicate_id = gold_document()
        duplicate_id["records"][1]["query_id"] = duplicate_id["records"][0][
            "query_id"
        ]
        cases.append(("query ids", duplicate_id, "duplicate gold query_id"))

        for label, document, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(evaluation.EvalError, message):
                    evaluation.validate_gold(document, expected_split="dev")

    def test_top_record_and_judgment_schemas_reject_unknown_or_missing_keys(self) -> None:
        cases: list[tuple[str, dict[str, object], str]] = []

        unknown_top = gold_document()
        unknown_top["surprise"] = True
        cases.append(("unknown top", unknown_top, "unknown keys"))

        missing_top = gold_document()
        del missing_top["split_methodology"]
        cases.append(("missing top", missing_top, "missing keys"))

        unknown_record = gold_document()
        unknown_record["records"][0]["surprise"] = True
        cases.append(("unknown record", unknown_record, "unknown keys"))

        missing_record = gold_document()
        del missing_record["records"][0]["scope"]
        cases.append(("missing record", missing_record, "missing keys"))

        unknown_judgment = gold_document()
        unknown_judgment["records"][0]["judgments"][0]["surprise"] = True
        cases.append(("unknown judgment", unknown_judgment, "unknown keys"))

        missing_judgment = gold_document()
        del missing_judgment["records"][0]["judgments"][0]["point_id"]
        cases.append(("missing judgment", missing_judgment, "missing"))

        for label, document, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(evaluation.EvalError, message):
                    evaluation.validate_gold(document, expected_split="dev")

    def test_judgment_identity_grade_and_interval_are_strict(self) -> None:
        mutations = (
            ("relative_path", None, "relative_path"),
            ("revision_id", 7, "revision_id"),
            ("span_id", "", "span_id"),
            ("point_id", None, "point_id"),
            ("span_sha256", "not-a-digest", "span"),
            ("grade", 0, "grade"),
            ("grade", 4, "grade"),
            ("byte_start", -1, "byte interval"),
            ("byte_end", 0, "byte interval"),
        )
        for field, value, message in mutations:
            document = gold_document()
            document["records"][0]["judgments"][0][field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(evaluation.EvalError, message):
                    evaluation.validate_gold(document, expected_split="dev")

    def test_answerability_and_expected_target_must_match_judgments(self) -> None:
        no_judgment = gold_document()
        no_judgment["records"][0]["judgments"] = []
        with self.assertRaisesRegex(evaluation.EvalError, "no positive judgment"):
            evaluation.validate_gold(no_judgment, expected_split="dev")

        negative_with_judgment = gold_document()
        negative_with_judgment["records"][-1]["judgments"] = [judgment(99)]
        with self.assertRaisesRegex(
            evaluation.EvalError,
            "unanswerable but has judgments",
        ):
            evaluation.validate_gold(negative_with_judgment, expected_split="dev")

        mismatched_target = gold_document()
        mismatched_target["records"][0][
            "expected_revision_id"
        ] = f"revision:{digest('not-the-judgment')}"
        with self.assertRaisesRegex(evaluation.EvalError, "expected target"):
            evaluation.validate_gold(mismatched_target, expected_split="dev")

    def test_split_digest_binds_queries_and_each_partition_independently(self) -> None:
        dev = evaluation.validate_gold(gold_document("dev"), "dev")
        holdout = evaluation.validate_gold(gold_document("holdout"), "holdout")
        changed = gold_document("dev")
        changed["records"][0]["query"] += " changed"
        changed_dev = evaluation.validate_gold(changed, "dev")

        self.assertNotEqual(dev.gold_digest, holdout.gold_digest)
        self.assertNotEqual(dev.gold_digest, changed_dev.gold_digest)
        self.assertEqual(dev.dev_digest, dev.gold_digest)
        self.assertIsNone(dev.holdout_digest)
        self.assertEqual(holdout.holdout_digest, holdout.gold_digest)
        self.assertIsNone(holdout.dev_digest)

    def test_scope_is_exactly_local_owner_current_only(self) -> None:
        document = gold_document()
        document["records"][0]["scope"] = "shared/current_only"

        with self.assertRaisesRegex(
            evaluation.EvalError,
            "scope must be exactly",
        ):
            evaluation.validate_gold(document, "dev")


class GoldBindingManifestTests(unittest.TestCase):
    def test_valid_manifest_has_exact_split_keys_hashes_counts_and_no_queries(
        self,
    ) -> None:
        document = gold_manifest_document()
        validated = evaluation.validate_gold_manifest(
            document,
            dev_digest=document["splits"]["dev"]["sha256"],
            holdout_digest=document["splits"]["holdout"]["sha256"],
        )

        self.assertEqual(set(validated), {"document", "manifest_digest"})
        self.assertEqual(
            set(validated["document"]["splits"]),
            {"dev", "holdout"},
        )
        for split in ("dev", "holdout"):
            self.assertEqual(
                set(validated["document"]["splits"][split]),
                {"file", "sha256", "record_count"},
            )
            self.assertEqual(
                validated["document"]["splits"][split]["record_count"],
                30,
            )
        serialized = json.dumps(validated["document"], sort_keys=True)
        self.assertNotIn('"records"', serialized)
        self.assertNotIn('"query"', serialized)
        self.assertNotIn('"judgments"', serialized)
        self.assertEqual(
            validated["manifest_digest"],
            hashlib.sha256(evaluation._canonical_json(document)).hexdigest(),
        )
        document["splits"]["dev"]["sha256"] = digest("mutated")
        self.assertNotEqual(
            validated["document"]["splits"]["dev"]["sha256"],
            document["splits"]["dev"]["sha256"],
        )

    def test_manifest_top_and_split_entry_keys_are_exact(self) -> None:
        cases: list[tuple[str, dict[str, object], str]] = []

        missing_top = gold_manifest_document()
        del missing_top["adjudication"]
        cases.append(("missing top", missing_top, "missing"))

        unknown_top = gold_manifest_document()
        unknown_top["records"] = []
        cases.append(("query-bearing top", unknown_top, "unknown keys"))

        missing_split_key = gold_manifest_document()
        del missing_split_key["splits"]["dev"]["record_count"]
        cases.append(("missing split key", missing_split_key, "incomplete"))

        unknown_split_key = gold_manifest_document()
        unknown_split_key["splits"]["holdout"]["query"] = "secret"
        cases.append(("query-bearing split", unknown_split_key, "unknown keys"))

        extra_split = gold_manifest_document()
        extra_split["splits"]["test"] = copy.deepcopy(
            extra_split["splits"]["dev"]
        )
        cases.append(("extra split", extra_split, "exactly dev and holdout"))

        for label, document, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(evaluation.EvalError, message):
                    evaluation.validate_gold_manifest(document)

    def test_manifest_digests_and_counts_are_strict(self) -> None:
        for split in ("dev", "holdout"):
            for value in ("short", "A" * 64, 7, None):
                document = gold_manifest_document()
                document["splits"][split]["sha256"] = value
                with self.subTest(split=split, digest=value):
                    with self.assertRaisesRegex(
                        evaluation.EvalError,
                        f"{split} digest",
                    ):
                        evaluation.validate_gold_manifest(document)

            for value in (29, 31, "30", None):
                document = gold_manifest_document()
                document["splits"][split]["record_count"] = value
                with self.subTest(split=split, count=value):
                    with self.assertRaisesRegex(
                        evaluation.EvalError,
                        f"{split} count",
                    ):
                        evaluation.validate_gold_manifest(document)

        invalid_span_digest = gold_manifest_document()
        invalid_span_digest["span_manifest_digest"] = "not-a-sha256"
        with self.assertRaisesRegex(
            evaluation.EvalError,
            "span_manifest_digest",
        ):
            evaluation.validate_gold_manifest(invalid_span_digest)

        document = gold_manifest_document()
        with self.assertRaisesRegex(evaluation.EvalError, "dev digest"):
            evaluation.validate_gold_manifest(
                document,
                dev_digest=digest("different-dev-bytes"),
            )
        with self.assertRaisesRegex(evaluation.EvalError, "holdout digest"):
            evaluation.validate_gold_manifest(
                document,
                holdout_digest=digest("different-holdout-bytes"),
            )

    def test_manifest_rejects_unsafe_or_ambiguous_split_filenames(self) -> None:
        unsafe = (
            "",
            ".",
            "..",
            "../dev.json",
            "nested/dev.json",
            "/tmp/dev.json",
        )
        for split in ("dev", "holdout"):
            for filename in unsafe:
                arguments = (
                    {"dev_file": filename}
                    if split == "dev"
                    else {"holdout_file": filename}
                )
                with self.subTest(split=split, filename=filename):
                    with self.assertRaisesRegex(
                        evaluation.EvalError,
                        f"{split} filename",
                    ):
                        evaluation.validate_gold_manifest(
                            gold_manifest_document(**arguments)
                        )

        same_name = gold_manifest_document(
            dev_file="same.json",
            holdout_file="same.json",
        )
        with self.assertRaisesRegex(evaluation.EvalError, "filenames"):
            evaluation.validate_gold_manifest(same_name)

    def test_load_manifest_requires_private_file_and_preserves_no_query_shape(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gold-manifest.json"
            document = gold_manifest_document()
            path.write_text(json.dumps(document), encoding="utf-8")
            path.chmod(0o600)

            loaded = evaluation.load_gold_manifest(path)

        self.assertEqual(loaded["document"], document)
        self.assertNotIn("records", loaded["document"])
        self.assertNotIn("query", json.dumps(loaded["document"]))


class RankingMetricTests(unittest.TestCase):
    def test_recall_mrr_and_ndcg_use_graded_exact_span_judgments(self) -> None:
        record = positive_record("dev", 1)
        best = judgment(101, grade=3)
        acceptable = judgment(102, grade=1)
        record["judgments"] = [best, acceptable]
        record["expected_relative_path"] = best["relative_path"]
        record["expected_revision_id"] = best["revision_id"]
        irrelevant = exact_result(judgment(103, grade=2))
        response = retrieval_response(
            [
                irrelevant,
                exact_result(acceptable),
                exact_result(best),
            ]
        )

        score = evaluation.score_query(record, response, k=10)

        dcg = 1.0 / math.log2(3) + 7.0 / math.log2(4)
        idcg = 7.0 / math.log2(2) + 1.0 / math.log2(3)
        self.assertEqual(score["recall_at_k"], 1.0)
        self.assertEqual(score["reciprocal_rank_at_k"], 0.5)
        self.assertAlmostEqual(score["ndcg_at_k"], dcg / idcg)
        self.assertTrue(score["citation_correct"])

    def test_repeated_relevant_span_cannot_earn_duplicate_ndcg_gain(self) -> None:
        record = positive_record("dev", 1)
        expected = record["judgments"][0]
        duplicate = copy.deepcopy(exact_result(expected))
        duplicate["point_id"] = "duplicate-point"
        response = retrieval_response([exact_result(expected), duplicate])

        score = evaluation.score_query(record, response, k=10)

        self.assertEqual(score["recall_at_k"], 1.0)
        self.assertEqual(score["reciprocal_rank_at_k"], 1.0)
        self.assertEqual(score["ndcg_at_k"], 1.0)
        self.assertEqual(score["duplicate_slots"], 1)

    def test_exact_citation_requires_every_bound_identity_and_hash_field(self) -> None:
        record = positive_record("dev", 1)
        expected = record["judgments"][0]
        valid = exact_result(expected)
        self.assertTrue(
            evaluation.score_query(
                record,
                retrieval_response([valid]),
            )["citation_correct"]
        )

        mutations = (
            ("relative_path", "plans/wrong.md"),
            ("revision_id", f"revision:{digest('wrong')}"),
            ("point_id", "wrong-point"),
            ("span_sha256", digest("wrong")),
            ("source_sha256_verified", False),
            ("span_sha256_verified", False),
            ("manifest_current", False),
        )
        for field, value in mutations:
            result = copy.deepcopy(valid)
            result[field] = value
            with self.subTest(field=field):
                self.assertFalse(
                    evaluation.score_query(
                        record,
                        retrieval_response([result]),
                    )["citation_correct"]
                )
        result = copy.deepcopy(valid)
        result["span"]["byte_end"] += 1
        self.assertFalse(
            evaluation.score_query(
                record,
                retrieval_response([result]),
            )["citation_correct"]
        )

    def test_hash_hard_negative_duplicate_and_leak_slots_are_exact(self) -> None:
        record = negative_record("dev", 1)
        historical_revision = f"revision:{digest('historical')}"
        record["expected_revision_id"] = historical_revision
        record["expected_relative_path"] = "plans/historical.md"
        record["hard_negative_paths"] = ["plans/historical.md"]
        leaked = {
            **exact_result(judgment(201)),
            "relative_path": "plans/historical.md",
            "revision_id": historical_revision,
            "manifest_current": False,
            "span_sha256_verified": False,
        }
        duplicate_one = {
            **exact_result(judgment(202)),
            "revision_id": "revision-current",
            "content_sha256": "same-content",
            "span_sha256": "same-span",
            "span": {"byte_start": 0, "byte_end": 100},
        }
        duplicate_two = {
            **duplicate_one,
            "span_id": "copy-two",
            "point_id": "copy-two",
        }
        duplicate_three = {
            **duplicate_one,
            "span_id": "copy-three",
            "point_id": "copy-three",
        }

        score = evaluation.score_query(
            record,
            retrieval_response(
                [leaked, duplicate_one, duplicate_two, duplicate_three]
            ),
        )

        self.assertEqual(score["hash_slots"], 4)
        self.assertEqual(score["verified_hash_slots"], 3)
        self.assertEqual(score["hard_negative_slots"], 1)
        self.assertEqual(score["historical_target_leaks"], 1)
        self.assertEqual(score["noncurrent_or_historical_slots"], 1)
        self.assertEqual(score["duplicate_slots"], 2)
        self.assertFalse(score["classification_correct"])

    def test_aggregate_metrics_are_macro_averaged_and_emit_p50_p95(self) -> None:
        positive = positive_record("dev", 1)
        negative = negative_record("dev", 1)
        responses = {
            positive["query_id"]: retrieval_response(
                [exact_result(positive["judgments"][0])]
            ),
            negative["query_id"]: retrieval_response([], abstained=True),
        }
        latencies = {
            positive["query_id"]: 40.0,
            negative["query_id"]: 10.0,
        }

        metrics = evaluation.aggregate_metrics(
            [positive, negative],
            responses,
            latencies,
        )

        self.assertEqual(metrics["recall_at_10"], 1.0)
        self.assertEqual(metrics["mrr_at_10"], 1.0)
        self.assertEqual(metrics["ndcg_at_10"], 1.0)
        self.assertEqual(metrics["exact_citation_accuracy"], 1.0)
        self.assertEqual(metrics["negative_abstention_accuracy"], 1.0)
        self.assertEqual(metrics["hash_verification_rate"], 1.0)
        self.assertEqual(metrics["latency_ms"]["p50"], 10.0)
        self.assertEqual(metrics["latency_ms"]["p95"], 40.0)

    def test_response_latency_and_result_shapes_fail_closed(self) -> None:
        positive = positive_record("dev", 1)
        negative = negative_record("dev", 1)
        query_id = str(positive["query_id"])
        negative_id = str(negative["query_id"])
        response = retrieval_response([exact_result(positive["judgments"][0])])
        negative_response = retrieval_response([], abstained=True)

        with self.assertRaisesRegex(evaluation.EvalError, "response ID mismatch"):
            evaluation.aggregate_metrics(
                [positive],
                {**{query_id: response}, "extra": response},
                [1.0],
            )
        for latency in (-1.0, math.nan, math.inf):
            with self.subTest(latency=latency):
                with self.assertRaises(evaluation.EvalError):
                    evaluation.aggregate_metrics(
                        [positive, negative],
                        {
                            query_id: response,
                            negative_id: negative_response,
                        },
                        [latency, 1.0],
                    )
        with self.assertRaisesRegex(evaluation.EvalError, "must be a list"):
            evaluation.score_query(
                positive,
                {"abstained": False, "results": "not-a-list"},
            )


class PercentileTests(unittest.TestCase):
    def test_nearest_rank_percentiles_are_deterministic(self) -> None:
        values = [100.0, 1.0, 50.0, 20.0, 10.0]
        original = list(values)

        self.assertEqual(evaluation.nearest_rank_percentile(values, 0.0), 1.0)
        self.assertEqual(evaluation.nearest_rank_percentile(values, 0.5), 20.0)
        self.assertEqual(evaluation.nearest_rank_percentile(values, 0.95), 100.0)
        self.assertEqual(evaluation.nearest_rank_percentile(values, 1.0), 100.0)
        self.assertEqual(values, original)
        self.assertEqual(evaluation.nearest_rank_percentile([], 0.95), 0.0)

    def test_percentile_rejects_invalid_probability_and_samples(self) -> None:
        for percentile in (-0.1, 1.1):
            with self.subTest(percentile=percentile):
                with self.assertRaisesRegex(evaluation.EvalError, "percentile"):
                    evaluation.nearest_rank_percentile([1.0], percentile)
        for sample in (-1.0, math.nan, math.inf):
            with self.subTest(sample=sample):
                with self.assertRaises(evaluation.EvalError):
                    evaluation.nearest_rank_percentile([sample], 0.95)


class AuditedResponses(Mapping[str, Mapping[str, object]]):
    def __init__(self, values: dict[str, Mapping[str, object]]):
        self.values = values
        self.reads: list[str] = []

    def __getitem__(self, key: str) -> Mapping[str, object]:
        self.reads.append(key)
        return self.values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


class PolicySelectionTests(unittest.TestCase):
    @staticmethod
    def policies() -> tuple[dict[str, float], dict[str, float]]:
        permissive = {
            "exact_identifier_cross_min": 0.5,
            "agreement_cross_min": 0.5,
            "single_channel_cross_min": 0.5,
            "cross_margin_min": 0.1,
        }
        strict = {
            "exact_identifier_cross_min": 0.9,
            "agreement_cross_min": 0.9,
            "single_channel_cross_min": 0.9,
            "cross_margin_min": 0.2,
        }
        return permissive, strict

    def test_policy_is_selected_only_from_development_classification(self) -> None:
        records = [
            positive_record("dev", 1),
            positive_record("dev", 2),
            negative_record("dev", 1),
            negative_record("dev", 2),
        ]
        responses: dict[str, Mapping[str, object]] = {}
        for record in records:
            if record["answerable"]:
                responses[str(record["query_id"])] = retrieval_response(
                    [exact_result(record["judgments"][0])],
                    cross_score=0.8,
                    cross_margin=0.3,
                )
            else:
                responses[str(record["query_id"])] = retrieval_response(
                    [],
                    cross_score=0.2,
                    cross_margin=0.01,
                )
        permissive, strict = self.policies()

        selected = evaluation.select_abstention_policy(
            records,
            responses,
            candidate_policies=[strict, permissive],
        )

        self.assertEqual(selected["policy"], permissive)
        self.assertTrue(selected["feasible"])
        self.assertEqual(
            selected["development_metrics"]["negative_abstention_accuracy"],
            1.0,
        )
        self.assertEqual(
            selected["development_metrics"]["recall_at_10"],
            1.0,
        )

    def test_holdout_records_are_rejected_before_any_response_is_read(self) -> None:
        holdout = [positive_record("holdout", 1)]
        response = retrieval_response(
            [exact_result(holdout[0]["judgments"][0])]
        )
        audited = AuditedResponses({str(holdout[0]["query_id"]): response})

        with self.assertRaisesRegex(
            evaluation.EvalError,
            "development records only",
        ):
            evaluation.select_abstention_policy(
                holdout,
                audited,
                candidate_policies=self.policies(),
            )
        self.assertEqual(audited.reads, [])

    def test_frozen_policy_digest_is_canonical_and_change_sensitive(self) -> None:
        policy, _strict = self.policies()
        reordered = {key: policy[key] for key in reversed(list(policy))}

        first = evaluation.freeze_policy(policy)
        repeated = evaluation.freeze_policy(reordered)
        changed = evaluation.freeze_policy(
            {**policy, "cross_margin_min": policy["cross_margin_min"] + 0.01}
        )

        self.assertEqual(first["policy_digest"], repeated["policy_digest"])
        self.assertNotEqual(first["policy_digest"], changed["policy_digest"])
        self.assertEqual(
            first["policy_digest"],
            hashlib.sha256(
                evaluation._canonical_json(first["policy"])
            ).hexdigest(),
        )
        self.assertEqual(first["ranking_version"], evaluation.retrieval.RANKING_VERSION)

    def test_frozen_policy_rejects_schema_drift_and_nonfinite_thresholds(self) -> None:
        policy, _strict = self.policies()
        for invalid in (
            {key: value for key, value in policy.items() if key != "cross_margin_min"},
            {**policy, "extra": 1.0},
            {**policy, "cross_margin_min": math.nan},
            {**policy, "cross_margin_min": math.inf},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(evaluation.EvalError):
                    evaluation.freeze_policy(invalid)


class MigrationEvidenceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.derived_patch = mock.patch.object(
            evaluation.artifact_runtime,
            "DEFAULT_DERIVED_ROOT",
            self.root,
        )
        self.derived_patch.start()
        self.manifest = self.root / "span-manifest.sqlite3"
        self.manifest.write_bytes(b"manifest")
        self.manifest.chmod(0o600)
        self.manifest_sha = evaluation._file_sha256(self.manifest)
        self.vector_leaves = [
            hashlib.sha256(f"vector-{index}".encode()).digest()
            for index in range(7)
        ]
        vector_digest = hashlib.sha256()
        for leaf in sorted(self.vector_leaves):
            vector_digest.update(leaf)
        self.vector_digest = vector_digest.hexdigest()
        self.migration_contract = evaluation.migration._migration_contract(
            batch_size=7,
            inference_batch_size=7,
            threads=1,
        )
        self.migration_contract_digest = hashlib.sha256(
            evaluation._canonical_json(self.migration_contract)
        ).hexdigest()
        self.state = self.root / "migration-state.sqlite3"
        with closing(sqlite3.connect(self.state)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE metadata (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE progress (
                  singleton INTEGER PRIMARY KEY,
                  last_row_id INTEGER NOT NULL,
                  points INTEGER NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE vector_attestations (
                  point_id TEXT PRIMARY KEY,
                  leaf_sha256 BLOB NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                {
                    "manifest_sha256": self.manifest_sha,
                    "collection": "spans",
                    "generation": "g20260718v2",
                    "model_manifest_digest": digest("embedding-model"),
                    "execution_contract_digest": (
                        self.migration_contract_digest
                    ),
                }.items(),
            )
            connection.execute(
                "INSERT INTO progress VALUES (1, 7, 7, '')"
            )
            connection.executemany(
                "INSERT INTO vector_attestations VALUES (?, ?)",
                [
                    (f"point-{index}", leaf)
                    for index, leaf in enumerate(self.vector_leaves)
                ],
            )
        self.state.chmod(0o600)
        self.document = {
            "schema_version": 1,
            "status": "passed",
            "manifest": {
                "path": str(self.manifest),
                "file_sha256": self.manifest_sha,
                "span_manifest_digest": digest("span-manifest"),
                "profile_digest": digest("profile"),
                "model_manifest_digest": digest("embedding-model"),
                "spans": 7,
            },
            "qdrant": {
                "collection": "spans",
                "generation": "g20260718v2",
                "points": 7,
                "content_payload": False,
                "point_set": {
                    "points": 7,
                    "span_manifest_digest": digest("span-manifest"),
                    "payload_contract": "exact-manifest-row-v1",
                    "vector_contract": (
                        "sorted-point-id-sha256-float32le-v1/"
                        f"{evaluation.retrieval.EMBEDDING_DIMENSIONS}"
                    ),
                    "vector_set_sha256": self.vector_digest,
                },
            },
            "embedding": {
                "model": evaluation.EMBEDDING_MODEL,
                "model_manifest_digest": digest("embedding-model"),
                "local_files_only": True,
                "publication_batch_size": 7,
                "inference_batch_size": 7,
                "threads": 1,
            },
            "migration_contract": self.migration_contract,
            "migration_contract_digest": self.migration_contract_digest,
            "checkpoint": {
                "path": str(self.state),
                "last_row_id": 7,
                "points": 7,
                "vector_attestations": 7,
                "vector_set_sha256": self.vector_digest,
            },
            "canonical_mutation": "disabled",
            "prior_generations_modified": False,
        }
        self.path = self.root / "migration-evidence.json"

    def tearDown(self) -> None:
        self.derived_patch.stop()
        self.temp.cleanup()

    def _write(self, document: Mapping[str, object], *, sign: bool = True) -> None:
        payload = copy.deepcopy(document)
        if sign:
            payload["evidence_digest"] = hashlib.sha256(
                evaluation._canonical_json(payload)
            ).hexdigest()
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.path.chmod(0o600)

    def _validate(self) -> dict[str, str]:
        return evaluation._validate_migration_evidence(
            self.path,
            manifest=self.manifest,
            manifest_sha256=self.manifest_sha,
            span_manifest_digest=digest("span-manifest"),
            profile_digest=digest("profile"),
            embedding_model_manifest_digest=digest("embedding-model"),
            collection="spans",
            generation="g20260718v2",
            expected_points=7,
        )

    @platform_skips.requires_qdrant_stack
    def test_passed_private_migration_evidence_binds_full_vector_set(self) -> None:
        self._write(self.document)

        binding = self._validate()

        self.assertEqual(binding["path"], str(self.path))
        self.assertEqual(binding["file_sha256"], evaluation._file_sha256(self.path))
        self.assertEqual(binding["vector_set_sha256"], self.vector_digest)
        self.assertEqual(
            binding["migration_contract_digest"],
            self.migration_contract_digest,
        )
        self.assertEqual(
            binding["evidence_digest"],
            json.loads(self.path.read_text(encoding="utf-8"))["evidence_digest"],
        )

    @platform_skips.requires_qdrant_stack
    def test_tamper_and_release_identity_mismatch_fail_closed(self) -> None:
        unsigned = copy.deepcopy(self.document)
        unsigned["status"] = "failed"
        self._write(unsigned)
        with self.assertRaisesRegex(evaluation.EvalError, "not passed"):
            self._validate()

        mismatched = copy.deepcopy(self.document)
        mismatched["qdrant"]["collection"] = "other"  # type: ignore[index]
        self._write(mismatched)
        with self.assertRaisesRegex(evaluation.EvalError, "Qdrant identity"):
            self._validate()

        mismatched = copy.deepcopy(self.document)
        mismatched["qdrant"]["point_set"]["vector_contract"] = "other"  # type: ignore[index]
        self._write(mismatched)
        with self.assertRaisesRegex(evaluation.EvalError, "Qdrant identity"):
            self._validate()

        self._write(self.document, sign=False)
        with self.assertRaisesRegex(evaluation.EvalError, "embedded evidence digest"):
            self._validate()


class HoldoutLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.derived = self.root / "derived"
        self.derived.mkdir(mode=0o700)
        self.derived_patch = mock.patch.object(
            evaluation.artifact_runtime,
            "DEFAULT_DERIVED_ROOT",
            self.derived,
        )
        self.derived_patch.start()
        self.arguments = {
            "gold_file_sha256": digest("holdout-file"),
            "generation": "g20260718v2",
            "span_manifest_digest": digest("span-manifest"),
            "policy_digest": digest("frozen-policy"),
            "system_digest": digest("system"),
        }

    def tearDown(self) -> None:
        self.derived_patch.stop()
        self.temp.cleanup()

    @platform_skips.requires_posix_modes
    def test_same_ledger_can_be_reserved_for_holdout_only_once(self) -> None:
        with (
            mock.patch.object(evaluation.time, "time", return_value=100.0),
            mock.patch.object(
                evaluation.security,
                "fsync_directory",
                wraps=evaluation.security.fsync_directory,
            ) as fsync_directory,
        ):
            ledger, first = evaluation._reserve_holdout(**self.arguments)

        self.assertEqual(first["state"], "reserved")
        self.assertEqual(
            ledger,
            evaluation._holdout_ledger_path(
                gold_file_sha256=self.arguments["gold_file_sha256"],
                generation=self.arguments["generation"],
                span_manifest_digest=self.arguments["span_manifest_digest"],
            ),
        )
        self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), 0o600)
        fsync_directory.assert_called_with(self.derived)
        with self.assertRaisesRegex(
            evaluation.EvalError,
            "already been reserved or evaluated",
        ):
            evaluation._reserve_holdout(**self.arguments)

    def test_completed_ledger_rejects_second_completion_or_reservation(self) -> None:
        with mock.patch.object(evaluation.time, "time", return_value=100.0):
            ledger, _reservation = evaluation._reserve_holdout(**self.arguments)
        with mock.patch.object(evaluation.time, "time", return_value=200.0):
            completed = evaluation.record_holdout_run(
                **self.arguments,
                gold_digest=digest("holdout-gold"),
                evidence_digest=digest("evidence"),
                evidence_file_sha256=digest("evidence-file"),
            )

        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["completed_unix"], 200.0)
        with self.assertRaisesRegex(evaluation.EvalError, "not in the reserved state"):
            evaluation.record_holdout_run(
                **self.arguments,
                gold_digest=digest("holdout-gold"),
                evidence_digest=digest("second-evidence"),
                evidence_file_sha256=digest("second-evidence-file"),
            )
        with self.assertRaisesRegex(
            evaluation.EvalError,
            "already been reserved or evaluated",
        ):
            evaluation._reserve_holdout(**self.arguments)
        self.assertEqual(
            json.loads(ledger.read_text(encoding="utf-8"))["gold_digest"],
            digest("holdout-gold"),
        )

    def test_ledger_identity_mismatch_fails_without_completing(self) -> None:
        ledger, _reservation = evaluation._reserve_holdout(**self.arguments)

        for field in ("policy_digest", "system_digest"):
            arguments = {**self.arguments, field: digest(f"wrong-{field}")}
            with self.subTest(field=field):
                with self.assertRaisesRegex(evaluation.EvalError, field):
                    evaluation.record_holdout_run(
                        **arguments,
                        gold_digest=digest("holdout-gold"),
                        evidence_digest=digest("evidence"),
                        evidence_file_sha256=digest("evidence-file"),
                    )
        self.assertEqual(
            json.loads(ledger.read_text(encoding="utf-8"))["state"],
            "reserved",
        )

    def test_canonical_ledger_changes_only_with_holdout_release_identity(self) -> None:
        original = evaluation._holdout_ledger_path(
            gold_file_sha256=self.arguments["gold_file_sha256"],
            generation=self.arguments["generation"],
            span_manifest_digest=self.arguments["span_manifest_digest"],
        )
        repeated = evaluation._holdout_ledger_path(
            gold_file_sha256=self.arguments["gold_file_sha256"],
            generation=self.arguments["generation"],
            span_manifest_digest=self.arguments["span_manifest_digest"],
        )
        changed = evaluation._holdout_ledger_path(
            gold_file_sha256=digest("other-holdout"),
            generation=self.arguments["generation"],
            span_manifest_digest=self.arguments["span_manifest_digest"],
        )

        self.assertEqual(original, repeated)
        self.assertNotEqual(original, changed)
        self.assertEqual(original.parent, self.derived)


class EvaluationGoldBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.derived_patch = mock.patch.object(
            evaluation.artifact_runtime,
            "DEFAULT_DERIVED_ROOT",
            self.root,
        )
        self.derived_patch.start()
        self.dev_path = self.root / "retrieval-gold-v1.dev.json"
        self.holdout_path = self.root / "retrieval-gold-v1.holdout.json"
        self.dev_document = gold_document("dev")
        self.holdout_document = gold_document("holdout")
        self._write_json(self.dev_path, self.dev_document)
        self._write_json(self.holdout_path, self.holdout_document)

    def tearDown(self) -> None:
        self.derived_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _write_json(
        path: Path,
        document: Mapping[str, object],
        *,
        pretty: bool = False,
    ) -> None:
        path.write_text(
            json.dumps(
                document,
                indent=2 if pretty else None,
                sort_keys=pretty,
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _binding(
        self,
        *,
        dev_path: Path | None = None,
        holdout_path: Path | None = None,
        dev_file: str | None = None,
        holdout_file: str | None = None,
    ) -> dict[str, object]:
        dev_path = dev_path or self.dev_path
        holdout_path = holdout_path or self.holdout_path
        return gold_manifest_document(
            dev_sha256=evaluation._file_sha256(dev_path),
            holdout_sha256=evaluation._file_sha256(holdout_path),
            dev_file=dev_file or dev_path.name,
            holdout_file=holdout_file or holdout_path.name,
        )

    def _arguments(
        self,
        *,
        split: str,
        gold_path: Path,
        binding_path: Path,
        policy_path: Path | None = None,
        output_path: Path | None = None,
    ) -> dict[str, object]:
        return {
            "split": split,
            "gold_path": gold_path,
            "gold_manifest_path": binding_path,
            "manifest": self.root / "span-manifest.sqlite3",
            "collection": "test-collection",
            "embedding_snapshot": self.root / "embedding-model",
            "reranker_snapshot": self.root / "reranker-model",
            "migration_evidence_path": self.root / "migration-evidence.json",
            "policy_path": policy_path or self.root / "policy.json",
            "output_path": output_path or self.root / f"{split}-evidence.json",
        }

    def test_evaluate_rejects_selected_split_filename_before_retrieval(self) -> None:
        selected = self.root / "selected.dev.json"
        self._write_json(selected, self.dev_document)
        binding_path = self.root / "gold-manifest.json"
        binding = self._binding(
            dev_path=selected,
            dev_file="expected.dev.json",
        )
        self._write_json(binding_path, binding)

        with mock.patch.object(evaluation, "_run_modes") as run_modes:
            with mock.patch.object(evaluation, "_manifest_metadata") as metadata:
                with self.assertRaisesRegex(
                    evaluation.EvalError,
                    "filename does not match",
                ):
                    evaluation.evaluate(
                        **self._arguments(
                            split="dev",
                            gold_path=selected,
                            binding_path=binding_path,
                        )
                    )
        run_modes.assert_not_called()
        metadata.assert_not_called()

    def test_evaluate_rejects_selected_split_bytes_before_retrieval(self) -> None:
        binding_path = self.root / "gold-manifest.json"
        binding = self._binding()
        self._write_json(binding_path, binding)
        # Semantically identical JSON, intentionally different frozen bytes.
        self._write_json(self.dev_path, self.dev_document, pretty=True)

        with mock.patch.object(evaluation, "_run_modes") as run_modes:
            with mock.patch.object(evaluation, "_manifest_metadata") as metadata:
                with self.assertRaisesRegex(
                    evaluation.EvalError,
                    "bytes do not match",
                ):
                    evaluation.evaluate(
                        **self._arguments(
                            split="dev",
                            gold_path=self.dev_path,
                            binding_path=binding_path,
                        )
                    )
        run_modes.assert_not_called()
        metadata.assert_not_called()

    @staticmethod
    def _mode_results(
        **kwargs: object,
    ) -> tuple[
        dict[str, dict[str, dict[str, object]]],
        dict[str, dict[str, float]],
        dict[str, object],
    ]:
        suite = kwargs["suite"]
        responses: dict[str, dict[str, dict[str, object]]] = {}
        latencies: dict[str, dict[str, float]] = {}
        for mode in evaluation.MODES:
            mode_responses: dict[str, dict[str, object]] = {}
            mode_latencies: dict[str, float] = {}
            for record in suite.records:
                query_id = str(record["query_id"])
                if record["answerable"]:
                    result = exact_result(record["judgments"][0])
                    response = retrieval_response(
                        [result],
                        cross_score=0.9,
                        cross_margin=0.5,
                    )
                else:
                    response = retrieval_response(
                        [],
                        cross_score=0.1,
                        cross_margin=0.0,
                    )
                mode_responses[query_id] = response
                mode_latencies[query_id] = 1.0
            responses[mode] = mode_responses
            latencies[mode] = mode_latencies
        metadata = {
            "generation": "g20260718v2",
            "profile_digest": digest("profile"),
            "span_manifest_digest": digest("span-manifest"),
            "manifest_file_sha256": digest("manifest-file"),
            "embedding_model": evaluation.EMBEDDING_MODEL,
            "embedding_model_manifest_digest": digest("embedding-model"),
            "content_payload": False,
            "current_only": True,
        }
        return responses, latencies, metadata

    @contextmanager
    def _patched_evaluation(
        self,
        *,
        selected_feasible: bool = True,
        mode_results: object | None = None,
    ) -> Iterator[SimpleNamespace]:
        selected_policy = {
            "exact_identifier_cross_min": 0.5,
            "agreement_cross_min": 0.5,
            "single_channel_cross_min": 0.5,
            "cross_margin_min": 0.1,
        }
        manifest_metadata = {
            "generation": "g20260718v2",
            "span_manifest_digest": digest("span-manifest"),
            "embedding_model_manifest_digest": digest("embedding-model"),
            "profile_digest": digest("profile"),
            "spans": 1,
        }
        collection_contract = {
            "vector_size": 384,
            "distance": "Cosine",
            "points": 1,
            "server_version": evaluation.retrieval.QDRANT_SERVER_VERSION,
            "point_set": {
                "points": 1,
                "span_manifest_digest": digest("span-manifest"),
                "payload_contract": "exact-manifest-row-v1",
                "vector_set_sha256": digest("vector-set"),
            },
            "metadata": {},
        }
        with (
            mock.patch.object(
                evaluation,
                "_manifest_metadata",
                return_value=(manifest_metadata, digest("manifest-file")),
            ),
            mock.patch.object(evaluation, "_validate_gold_judgments"),
            mock.patch.object(
                evaluation,
                "_path_manifest",
                side_effect=lambda path: (
                    [],
                    (
                        digest("embedding-model")
                        if "embedding" in path.name
                        else digest("reranker-model")
                    ),
                ),
            ),
            mock.patch.object(
                evaluation.artifact_runtime,
                "load_runtime",
                return_value=SimpleNamespace(catalog=self.root / "catalog.sqlite3"),
            ),
            mock.patch.object(
                evaluation,
                "_catalog_release_binding",
                return_value={
                    "catalog_run_id": 18,
                    "catalog_artifacts": 1,
                    "catalog_revision_set_sha256": digest("catalog"),
                },
            ),
            mock.patch.object(
                evaluation,
                "_validate_migration_evidence",
                return_value={
                    "path": str(self.root / "migration-evidence.json"),
                    "file_sha256": digest("migration-file"),
                    "evidence_digest": digest("migration-evidence"),
                    "vector_set_sha256": digest("vector-set"),
                },
            ),
            mock.patch.object(
                evaluation,
                "select_abstention_policy",
                return_value={
                    "policy": selected_policy,
                    "feasible": selected_feasible,
                    "development_metrics": {},
                },
            ),
            mock.patch.object(
                evaluation,
                "_preflight_collection",
                return_value=collection_contract,
            ) as preflight,
            mock.patch.object(
                evaluation,
                "_run_modes",
                side_effect=mode_results or self._mode_results,
            ) as run_modes,
        ):
            yield SimpleNamespace(
                run_modes=run_modes,
                preflight=preflight,
                policy=selected_policy,
            )

    @platform_skips.requires_qdrant_stack
    def test_dev_frozen_system_binds_both_split_hashes_and_rejects_swap(
        self,
    ) -> None:
        original_binding_path = self.root / "gold-manifest.json"
        original_binding = self._binding()
        self._write_json(original_binding_path, original_binding)
        policy_path = self.root / "frozen-policy.json"
        dev_output = self.root / "dev-evidence.json"
        selected_policy = {
            "exact_identifier_cross_min": 0.5,
            "agreement_cross_min": 0.5,
            "single_channel_cross_min": 0.5,
            "cross_margin_min": 0.1,
        }
        manifest_metadata = {
            "generation": "g20260718v2",
            "span_manifest_digest": digest("span-manifest"),
            "embedding_model_manifest_digest": digest("embedding-model"),
            "profile_digest": digest("profile"),
            "spans": 1,
        }

        with (
            mock.patch.object(
                evaluation,
                "_manifest_metadata",
                return_value=(manifest_metadata, digest("manifest-file")),
            ),
            mock.patch.object(evaluation, "_validate_gold_judgments"),
            mock.patch.object(
                evaluation,
                "_path_manifest",
                side_effect=lambda path: (
                    [],
                    (
                        digest("embedding-model")
                        if "embedding" in path.name
                        else digest("reranker-model")
                    ),
                ),
            ),
            mock.patch.object(
                evaluation.artifact_runtime,
                "load_runtime",
                return_value=SimpleNamespace(catalog=self.root / "catalog.sqlite3"),
            ),
            mock.patch.object(
                evaluation,
                "_catalog_release_binding",
                return_value={
                    "catalog_run_id": 18,
                    "catalog_artifacts": 1,
                    "catalog_revision_set_sha256": digest("catalog"),
                },
            ),
            mock.patch.object(
                evaluation,
                "_validate_migration_evidence",
                return_value={
                    "path": str(self.root / "migration-evidence.json"),
                    "file_sha256": digest("migration-file"),
                    "evidence_digest": digest("migration-evidence"),
                    "vector_set_sha256": digest("vector-set"),
                },
            ),
            mock.patch.object(
                evaluation,
                "select_abstention_policy",
                return_value={
                    "policy": selected_policy,
                    "feasible": True,
                    "development_metrics": {},
                },
            ),
            mock.patch.object(
                evaluation,
                "_preflight_collection",
                return_value={
                    "vector_size": 384,
                    "distance": "Cosine",
                    "points": 1,
                    "server_version": evaluation.retrieval.QDRANT_SERVER_VERSION,
                    "point_set": {
                        "points": 1,
                        "span_manifest_digest": digest("span-manifest"),
                        "payload_contract": "exact-manifest-row-v1",
                        "vector_set_sha256": digest("vector-set"),
                    },
                    "metadata": {},
                },
            ),
            mock.patch.object(
                evaluation,
                "_run_modes",
                side_effect=self._mode_results,
            ) as run_modes,
        ):
            dev_evidence = evaluation.evaluate(
                **self._arguments(
                    split="dev",
                    gold_path=self.dev_path,
                    binding_path=original_binding_path,
                    policy_path=policy_path,
                    output_path=dev_output,
                )
            )

            frozen = json.loads(policy_path.read_text(encoding="utf-8"))
            expected_hashes = {
                "dev": evaluation._file_sha256(self.dev_path),
                "holdout": evaluation._file_sha256(self.holdout_path),
            }
            self.assertEqual(
                frozen["system"]["gold_split_file_sha256"],
                expected_hashes,
            )
            self.assertEqual(
                dev_evidence["system"]["gold_split_file_sha256"],
                expected_hashes,
            )
            self.assertEqual(
                frozen["system_digest"],
                hashlib.sha256(
                    evaluation._canonical_json(frozen["system"])
                ).hexdigest(),
            )
            self.assertEqual(
                set(frozen["system"]["material_sources"]["files"][index]["path"]
                    for index in range(
                        len(frozen["system"]["material_sources"]["files"])
                    )),
                set(evaluation.MATERIAL_SOURCE_FILES)
                | {label for label, _path in evaluation.MATERIAL_ADAPTER_FILES},
            )
            self.assertEqual(
                frozen["development_evidence"],
                {
                    "path": str(dev_output),
                    "file_sha256": evaluation._file_sha256(dev_output),
                    "evidence_digest": dev_evidence["evidence_digest"],
                },
            )
            self.assertEqual(
                evaluation._load_release_policy(
                    policy_path,
                    system=dev_evidence["system"],
                    system_digest=dev_evidence["system_digest"],
                ),
                frozen,
            )

            # Swap only the holdout bytes and update their hash-only binding.
            # The selected dev file and all retrieval/model inputs stay fixed.
            self._write_json(
                self.holdout_path,
                self.holdout_document,
                pretty=True,
            )
            swapped_binding_path = self.root / "swapped-gold-manifest.json"
            swapped_binding = self._binding()
            self._write_json(swapped_binding_path, swapped_binding)

            with self.assertRaisesRegex(
                evaluation.EvalError,
                "frozen development policy does not match this system",
            ):
                evaluation.evaluate(
                    **self._arguments(
                        split="holdout",
                        gold_path=self.holdout_path,
                        binding_path=swapped_binding_path,
                        policy_path=policy_path,
                        output_path=self.root / "holdout-evidence.json",
                    )
                )

        # Only the development retrieval ran; the swapped holdout was rejected
        # at the frozen system identity boundary before reservation/read.
        self.assertEqual(run_modes.call_count, 1)
        self.assertEqual(
            list(self.root.glob("artifact-retrieval-holdout-*.json")),
            [],
        )

    @platform_skips.requires_qdrant_stack
    def test_failed_development_gate_publishes_evidence_but_no_policy(self) -> None:
        binding_path = self.root / "gold-manifest.json"
        self._write_json(binding_path, self._binding())
        policy_path = self.root / "failed-policy.json"
        output_path = self.root / "failed-dev-evidence.json"

        def failed_modes(**kwargs: object) -> tuple[
            dict[str, dict[str, dict[str, object]]],
            dict[str, dict[str, float]],
            dict[str, object],
        ]:
            responses, latencies, metadata = self._mode_results(**kwargs)
            for mode_responses in responses.values():
                for query_id in list(mode_responses):
                    mode_responses[query_id] = retrieval_response([])
            return responses, latencies, metadata

        with self._patched_evaluation(
            selected_feasible=False,
            mode_results=failed_modes,
        ):
            evidence = evaluation.evaluate(
                **self._arguments(
                    split="dev",
                    gold_path=self.dev_path,
                    binding_path=binding_path,
                    policy_path=policy_path,
                    output_path=output_path,
                )
            )

        self.assertEqual(evidence["status"], "failed")
        self.assertTrue(output_path.exists())
        self.assertFalse(policy_path.exists())

    @platform_skips.requires_qdrant_stack
    def test_collection_postflight_change_blocks_dev_evidence_and_policy(self) -> None:
        binding_path = self.root / "gold-manifest.json"
        self._write_json(binding_path, self._binding())
        policy_path = self.root / "policy.json"
        output_path = self.root / "dev-evidence.json"

        with self._patched_evaluation() as patched:
            initial = copy.deepcopy(patched.preflight.return_value)
            changed = copy.deepcopy(initial)
            changed["point_set"]["vector_set_sha256"] = digest("changed-vectors")
            patched.preflight.side_effect = [initial, changed]
            with self.assertRaisesRegex(
                evaluation.EvalError,
                "changed during evaluation",
            ):
                evaluation.evaluate(
                    **self._arguments(
                        split="dev",
                        gold_path=self.dev_path,
                        binding_path=binding_path,
                        policy_path=policy_path,
                        output_path=output_path,
                    )
                )

        self.assertFalse(output_path.exists())
        self.assertFalse(policy_path.exists())

    @platform_skips.requires_qdrant_stack
    def test_holdout_is_first_read_after_durable_reservation_and_failure_burns(
        self,
    ) -> None:
        binding_path = self.root / "gold-manifest.json"
        binding = self._binding()
        self._write_json(binding_path, binding)
        policy_path = self.root / "policy.json"
        dev_output = self.root / "dev-evidence.json"
        with self._patched_evaluation():
            evaluation.evaluate(
                **self._arguments(
                    split="dev",
                    gold_path=self.dev_path,
                    binding_path=binding_path,
                    policy_path=policy_path,
                    output_path=dev_output,
                )
            )

        holdout_binding = binding["splits"]["holdout"]
        ledger = evaluation._holdout_ledger_path(
            gold_file_sha256=holdout_binding["sha256"],
            generation=binding["generation"],
            span_manifest_digest=binding["span_manifest_digest"],
        )
        original_file_sha = evaluation._file_sha256
        holdout_byte_reads: list[Path] = []

        def audited_file_sha(path: Path) -> str:
            if Path(path) == self.holdout_path:
                self.assertTrue(ledger.exists())
                holdout_byte_reads.append(Path(path))
            return original_file_sha(path)

        def burn_after_reservation(path: Path, split: str) -> object:
            self.assertEqual(Path(path), self.holdout_path)
            self.assertEqual(split, "holdout")
            self.assertTrue(ledger.exists())
            raise RuntimeError("synthetic post-reservation crash")

        holdout_arguments = self._arguments(
            split="holdout",
            gold_path=self.holdout_path,
            binding_path=binding_path,
            policy_path=policy_path,
            output_path=self.root / "holdout-evidence.json",
        )
        with (
            self._patched_evaluation() as patched,
            mock.patch.object(
                evaluation,
                "_file_sha256",
                side_effect=audited_file_sha,
            ),
            mock.patch.object(
                evaluation,
                "load_gold",
                side_effect=burn_after_reservation,
            ) as load_gold,
        ):
            with self.assertRaisesRegex(RuntimeError, "post-reservation"):
                evaluation.evaluate(**holdout_arguments)

            self.assertEqual(holdout_byte_reads, [self.holdout_path])
            self.assertEqual(load_gold.call_count, 1)
            patched.run_modes.assert_not_called()
            self.assertEqual(
                json.loads(ledger.read_text(encoding="utf-8"))["state"],
                "reserved",
            )

            with self.assertRaisesRegex(
                evaluation.EvalError,
                "already been reserved or evaluated",
            ):
                evaluation.evaluate(**holdout_arguments)
            self.assertEqual(holdout_byte_reads, [self.holdout_path])
            self.assertEqual(load_gold.call_count, 1)


class ReleaseIdentityTests(unittest.TestCase):
    @platform_skips.requires_qdrant_stack
    def test_material_sources_and_runtime_dependencies_are_release_bound(self) -> None:
        source_manifest = evaluation._material_source_manifest()
        runtime_contract = evaluation._evaluation_runtime_contract()

        self.assertEqual(
            {entry["path"] for entry in source_manifest["files"]},
            set(evaluation.MATERIAL_SOURCE_FILES)
            | {label for label, _path in evaluation.MATERIAL_ADAPTER_FILES},
        )
        self.assertEqual(
            source_manifest["digest"],
            hashlib.sha256(
                evaluation._canonical_json(source_manifest["files"])
            ).hexdigest(),
        )
        self.assertEqual(
            set(runtime_contract["dependencies"]),
            {
                "fastembed",
                "numpy",
                "onnxruntime",
                "qdrant-client",
                "tokenizers",
            },
        )
        self.assertEqual(runtime_contract["top_k"], evaluation.TOP_K)
        self.assertEqual(runtime_contract["modes"], list(evaluation.MODES))

    def test_cli_requires_migration_evidence_and_exposes_no_ledger_override(
        self,
    ) -> None:
        parser = evaluation._parser()
        option_strings = {
            option
            for action in parser._actions
            for option in action.option_strings
        }
        migration = next(
            action
            for action in parser._actions
            if "--migration-evidence" in action.option_strings
        )

        self.assertTrue(migration.required)
        self.assertNotIn("--ledger", option_strings)

    def test_all_modes_receive_the_same_explicit_current_only_filters(self) -> None:
        class FakeEmbedding:
            def __init__(self, **_kwargs: object):
                pass

            @staticmethod
            def query_embed(_query: str) -> Iterator[SimpleNamespace]:
                yield SimpleNamespace(tolist=lambda: [0.0] * 384)

        class FakeCrossEncoder:
            def __init__(self, *_args: object, **_kwargs: object):
                pass

        client = SimpleNamespace(
            get_collection=lambda _collection: SimpleNamespace(
                config=SimpleNamespace(metadata={"generation": "g"})
            ),
            close=lambda: None,
        )
        retriever = mock.Mock()
        retriever.search.return_value = retrieval_response([])
        suite = SimpleNamespace(
            generation="g20260718v2",
            records=[positive_record("dev", 1)],
        )
        runtime = SimpleNamespace(
            workspace=Path("/workspace"),
            catalog=Path("/catalog"),
            qdrant_url="http://127.0.0.1:6333",
            qdrant_read_key=lambda: "read-key",
        )

        with (
            mock.patch.dict(
                sys.modules,
                {
                    "fastembed": SimpleNamespace(TextEmbedding=FakeEmbedding),
                    "fastembed.rerank": SimpleNamespace(),
                    "fastembed.rerank.cross_encoder": SimpleNamespace(
                        TextCrossEncoder=FakeCrossEncoder
                    ),
                    "qdrant_client": SimpleNamespace(
                        QdrantClient=lambda **_kwargs: client
                    ),
                },
            ),
            mock.patch.object(evaluation.retrieval, "LexicalIndex"),
            mock.patch.object(
                evaluation.retrieval,
                "HybridRetriever",
                return_value=retriever,
            ),
            mock.patch.object(
                evaluation,
                "_annotate_current",
                side_effect=lambda _manifest, response: response,
            ),
        ):
            evaluation._run_modes(
                suite=suite,
                manifest=Path("/manifest"),
                runtime=runtime,
                collection="spans",
                embedding_snapshot=Path("/embedding"),
                reranker_snapshot=Path("/reranker"),
            )

        self.assertEqual(retriever.search.call_count, len(evaluation.MODES))
        self.assertEqual(
            {call.kwargs["mode"] for call in retriever.search.call_args_list},
            set(evaluation.MODES),
        )
        for call in retriever.search.call_args_list:
            self.assertEqual(
                {
                    key: call.kwargs[key]
                    for key in evaluation.CURRENT_ONLY_FILTERS
                },
                evaluation.CURRENT_ONLY_FILTERS,
            )


class CollectionPreflightTests(unittest.TestCase):
    def _preflight(
        self,
        *,
        size: int = 384,
        distance: str = "Cosine",
        points: int = 7,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        client = SimpleNamespace(
            info=lambda: SimpleNamespace(
                version=evaluation.retrieval.QDRANT_SERVER_VERSION
            ),
            get_collection=lambda _collection: SimpleNamespace(
                config=SimpleNamespace(
                    metadata=dict(metadata or {"generation": "g"}),
                    params=SimpleNamespace(
                        vectors=SimpleNamespace(
                            size=size,
                            distance=distance,
                        )
                    ),
                )
            ),
            count=lambda **_kwargs: SimpleNamespace(count=points),
            close=lambda: None,
        )
        qdrant_module = SimpleNamespace(
            QdrantClient=lambda **_kwargs: client,
        )
        runtime = SimpleNamespace(
            qdrant_url="http://127.0.0.1:6333",
            qdrant_read_key=lambda: "read-key",
        )
        with mock.patch.dict(
            sys.modules,
            {"qdrant_client": qdrant_module},
        ), mock.patch.object(
            evaluation.retrieval,
            "verify_qdrant_point_set",
            return_value={
                "points": 7,
                "span_manifest_digest": digest("spans"),
                "payload_contract": "exact-manifest-row-v1",
                "vector_set_sha256": digest("vector-set"),
            },
        ):
            return evaluation._preflight_collection(
                runtime=runtime,
                collection="spans",
                manifest=Path("/unused"),
                expected_metadata={"generation": "g"},
                expected_points=7,
                expected_vector_set_sha256=digest("vector-set"),
            )

    def test_accepts_only_the_exact_evaluated_collection_contract(self) -> None:
        observed = self._preflight()
        self.assertEqual(observed["vector_size"], 384)
        self.assertEqual(observed["distance"], "Cosine")
        self.assertEqual(observed["points"], 7)

    def test_rejects_a_different_vector_space(self) -> None:
        with self.assertRaisesRegex(
            evaluation.EvalError,
            "384-dimensional cosine",
        ):
            self._preflight(distance="Dot")
        with self.assertRaisesRegex(
            evaluation.EvalError,
            "384-dimensional cosine",
        ):
            self._preflight(size=128)

    def test_rejects_partial_or_metadata_mismatched_collection(self) -> None:
        with self.assertRaisesRegex(
            evaluation.EvalError,
            "point count does not match",
        ):
            self._preflight(points=6)
        with self.assertRaisesRegex(
            evaluation.EvalError,
            "metadata generation mismatch",
        ):
            self._preflight(metadata={"generation": "other"})


if __name__ == "__main__":
    unittest.main()
