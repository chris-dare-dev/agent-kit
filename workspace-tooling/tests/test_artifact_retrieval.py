from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

import platform_skips
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_ingestion as ingestion  # noqa: E402
import artifact_retrieval as retrieval  # noqa: E402
import artifact_span_generation as span_generation  # noqa: E402


def catalog_artifact(
    relative_path: str,
    path: Path,
    *,
    content_sha256: str | None = None,
    byte_size: int | None = None,
    mtime_ns: int | None = None,
) -> ingestion.CatalogArtifact:
    raw = path.read_bytes()
    stat_result = path.stat()
    return ingestion.CatalogArtifact(
        artifact_id="artifact-1",
        revision_id="revision-1",
        relative_path=relative_path,
        content_sha256=content_sha256 or hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw) if byte_size is None else byte_size,
        mtime_ns=stat_result.st_mtime_ns if mtime_ns is None else mtime_ns,
        artifact_type="plan",
        authority_class="working",
        lifecycle_hints=("active",),
        source_scope="workspace",
        repository=None,
        project="test-project",
    )


def manifest_row(
    raw: bytes,
    start: int,
    end: int,
    *,
    relative_path: str = "plans/example.md",
    content_sha256: str | None = None,
) -> dict[str, object]:
    content = raw[start:end]
    prefix = raw[:start]
    try:
        char_start = len(prefix.decode("utf-8"))
        char_end = char_start + len(content.decode("utf-8"))
    except UnicodeDecodeError:
        # Some negative tests intentionally cut a multibyte code point.
        char_start = 0
        char_end = 1
    return {
        "artifact_id": "artifact-1",
        "revision_id": "revision-1",
        "relative_path": relative_path,
        "content_sha256": content_sha256 or hashlib.sha256(raw).hexdigest(),
        "byte_start": start,
        "byte_end": end,
        "char_start": char_start,
        "char_end": char_end,
        "line_start": prefix.count(b"\n") + 1,
        "line_end": raw[: max(start, end - 1)].count(b"\n") + 1,
        "span_sha256": hashlib.sha256(content).hexdigest(),
    }


def candidate(
    *,
    span_id: str,
    revision_id: str = "revision-1",
    content_sha256: str = "content-a",
    span_sha256: str = "span-a",
    byte_start: int = 0,
    byte_end: int = 100,
) -> retrieval.Candidate:
    return retrieval.Candidate(
        point_id=f"point-{span_id}",
        span_id=span_id,
        row={
            "revision_id": revision_id,
            "content_sha256": content_sha256,
            "span_sha256": span_sha256,
            "byte_start": byte_start,
            "byte_end": byte_end,
        },
    )


class QueryGrammarTests(unittest.TestCase):
    def test_safe_query_normalizes_and_enforces_bounds(self) -> None:
        self.assertEqual(retrieval._safe_query("  Ａlpha  "), "Alpha")
        for value in ("", "   ", "contains\0nul", "x" * 1001):
            with self.subTest(value=value[:20]):
                with self.assertRaises(retrieval.RetrievalError):
                    retrieval._safe_query(value)
        self.assertEqual(retrieval._safe_query("x" * 1000), "x" * 1000)

    def test_fts_expression_is_quoted_deduplicated_and_bounded(self) -> None:
        expression = retrieval._fts_query(
            'Alpha alpha" OR beta* NEAR(foo) repo:name -- [column]'
        )

        self.assertEqual(
            expression,
            '"alpha" OR "beta" OR "near" OR "foo" '
            'OR "repo:name" OR "column"',
        )
        terms = retrieval._query_terms(
            " ".join(f"token-{index}" for index in range(100))
        )
        self.assertEqual(len(terms), 64)
        self.assertTrue(all(len(term) <= 128 for term in terms))
        self.assertIsNone(retrieval._fts_query("🚀 !!!"))

    def test_fts_metacharacters_cannot_escape_term_phrases(self) -> None:
        query = 'alpha" OR * NOT {beta} NEAR(gamma) column:value'
        expression = retrieval._fts_query(query)

        self.assertIsNotNone(expression)
        self.assertNotIn("*", expression)
        self.assertNotIn("(", expression)
        self.assertNotIn(")", expression)
        self.assertTrue(
            all(
                part.startswith('"') and part.endswith('"')
                for part in expression.split(" OR ")
            )
        )


class DuplicateCollapseTests(unittest.TestCase):
    def test_same_copy_requires_hash_and_exact_interval(self) -> None:
        original = candidate(span_id="one")
        exact_copy = candidate(span_id="two", revision_id="revision-elsewhere")
        different_interval = candidate(span_id="three", byte_end=99)
        different_hash = candidate(span_id="four", span_sha256="span-b")

        self.assertTrue(retrieval._same_copy(original, exact_copy))
        self.assertFalse(retrieval._same_copy(original, different_interval))
        self.assertFalse(retrieval._same_copy(original, different_hash))

    def test_overlap_duplicate_is_revision_scoped_and_uses_union_ratio(self) -> None:
        original = candidate(span_id="one", byte_start=0, byte_end=100)
        eighty_percent = candidate(
            span_id="two",
            byte_start=0,
            byte_end=80,
            span_sha256="span-b",
        )
        below_threshold = candidate(
            span_id="three",
            byte_start=0,
            byte_end=79,
            span_sha256="span-c",
        )
        other_revision = candidate(
            span_id="four",
            revision_id="revision-2",
            byte_start=0,
            byte_end=100,
        )
        disjoint = candidate(
            span_id="five",
            byte_start=100,
            byte_end=200,
        )

        self.assertTrue(retrieval._overlap_duplicate(original, eighty_percent))
        self.assertFalse(retrieval._overlap_duplicate(original, below_threshold))
        self.assertFalse(retrieval._overlap_duplicate(original, other_revision))
        self.assertFalse(retrieval._overlap_duplicate(original, disjoint))


class CanonicalSpanVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "plans").mkdir()
        self.relative_path = "plans/example.md"
        self.path = self.workspace / self.relative_path
        self.raw = "# Café\r\n\nSecond 🚀 line\n".encode("utf-8")
        self.path.write_bytes(self.raw)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_valid_interval_returns_verified_utf8_and_exact_coordinates(self) -> None:
        start = self.raw.index("Café".encode("utf-8"))
        end = self.raw.index(b"\nSecond")
        row = manifest_row(self.raw, start, end)
        artifact = catalog_artifact(self.relative_path, self.path)

        result = retrieval._verified_canonical_span(
            workspace=self.workspace,
            artifact=artifact,
            row=row,
        )

        self.assertEqual(result["content"], "Café\r\n")
        self.assertEqual(
            result["span"],
            {
                "char_start": 2,
                "char_end": 8,
                "byte_start": start,
                "byte_end": end,
                "line_start": 1,
                "line_end": 1,
                "byte_interval": "half-open",
                "line_interval": "one-based-inclusive",
            },
        )
        self.assertTrue(result["source_sha256_verified"])
        self.assertTrue(result["span_sha256_verified"])

    def test_source_hash_mismatch_fails_closed(self) -> None:
        artifact = catalog_artifact(
            self.relative_path,
            self.path,
            content_sha256="0" * 64,
        )
        with self.assertRaisesRegex(
            retrieval.RetrievalError,
            "no longer matches the catalog",
        ):
            retrieval._verified_canonical_span(
                workspace=self.workspace,
                artifact=artifact,
                row=manifest_row(
                    self.raw,
                    0,
                    len(self.raw),
                    content_sha256="0" * 64,
                ),
            )

    def test_catalog_mtime_mismatch_fails_closed(self) -> None:
        artifact = catalog_artifact(
            self.relative_path,
            self.path,
            mtime_ns=self.path.stat().st_mtime_ns + 1,
        )
        with self.assertRaisesRegex(
            retrieval.RetrievalError,
            "no longer matches the catalog",
        ):
            retrieval._verified_canonical_span(
                workspace=self.workspace,
                artifact=artifact,
                row=manifest_row(
                    self.raw,
                    0,
                    len(self.raw),
                ),
            )

    @platform_skips.requires_symlinks
    def test_symlink_in_canonical_path_is_rejected(self) -> None:
        target = self.workspace / "actual.md"
        target.write_bytes(self.raw)
        link = self.workspace / "linked.md"
        link.symlink_to(target)
        artifact = catalog_artifact("linked.md", link)

        with self.assertRaisesRegex(
            retrieval.RetrievalError,
            "contains a symlink",
        ):
            retrieval._verified_canonical_span(
                workspace=self.workspace,
                artifact=artifact,
                row=manifest_row(
                    self.raw,
                    0,
                    len(self.raw),
                    relative_path="linked.md",
                ),
            )

    def test_out_of_bounds_and_empty_intervals_are_rejected(self) -> None:
        artifact = catalog_artifact(self.relative_path, self.path)
        for start, end in (
            (0, len(self.raw) + 1),
            (-1, 1),
            (4, 4),
            (5, 4),
        ):
            with self.subTest(start=start, end=end):
                row = manifest_row(self.raw, max(start, 0), min(end, len(self.raw)))
                row["byte_start"] = start
                row["byte_end"] = end
                with self.assertRaisesRegex(
                    retrieval.RetrievalError,
                    "interval is out of bounds",
                ):
                    retrieval._verified_canonical_span(
                        workspace=self.workspace,
                        artifact=artifact,
                        row=row,
                    )

    def test_span_hash_mismatch_is_rejected(self) -> None:
        artifact = catalog_artifact(self.relative_path, self.path)
        row = manifest_row(self.raw, 0, len(self.raw))
        row["span_sha256"] = "f" * 64

        with self.assertRaisesRegex(
            retrieval.RetrievalError,
            "span hash does not match",
        ):
            retrieval._verified_canonical_span(
                workspace=self.workspace,
                artifact=artifact,
                row=row,
            )

    def test_interval_that_splits_utf8_codepoint_is_rejected(self) -> None:
        artifact = catalog_artifact(self.relative_path, self.path)
        rocket = self.raw.index("🚀".encode("utf-8"))
        row = manifest_row(self.raw, rocket, rocket + 1)

        with self.assertRaisesRegex(
            retrieval.RetrievalError,
            "interval is not valid UTF-8",
        ):
            retrieval._verified_canonical_span(
                workspace=self.workspace,
                artifact=artifact,
                row=row,
            )

    def test_manifest_coordinate_mismatch_is_rejected(self) -> None:
        artifact = catalog_artifact(self.relative_path, self.path)
        row = manifest_row(self.raw, 0, len(self.raw))
        row["line_end"] = int(row["line_end"]) + 1

        with self.assertRaisesRegex(
            retrieval.RetrievalError,
            "coordinates do not match",
        ):
            retrieval._verified_canonical_span(
                workspace=self.workspace,
                artifact=artifact,
                row=row,
            )

    def test_size_ceiling_is_checked_before_opening_source(self) -> None:
        artifact = catalog_artifact(
            self.relative_path,
            self.path,
            byte_size=len(self.raw) + 1,
        )
        with mock.patch.object(retrieval, "MAX_CANONICAL_BYTES", len(self.raw)):
            with mock.patch.object(retrieval.os, "open") as open_file:
                with self.assertRaisesRegex(
                    retrieval.RetrievalError,
                    "exceeds the retrieval size limit",
                ):
                    retrieval._verified_canonical_span(
                        workspace=self.workspace,
                        artifact=artifact,
                        row=manifest_row(self.raw, 0, len(self.raw)),
                    )
        open_file.assert_not_called()

    def test_false_manifest_provenance_is_rejected_before_source_read(self) -> None:
        artifact = catalog_artifact(self.relative_path, self.path)
        baseline = manifest_row(self.raw, 0, len(self.raw))
        for field, value in (
            ("artifact_id", "other-artifact"),
            ("revision_id", "other-revision"),
            ("relative_path", "plans/other.md"),
            ("content_sha256", "f" * 64),
        ):
            with self.subTest(field=field):
                row = dict(baseline)
                row[field] = value
                with mock.patch.object(retrieval.os, "open") as open_file:
                    with self.assertRaisesRegex(
                        retrieval.RetrievalError,
                        "provenance does not match",
                    ):
                        retrieval._verified_canonical_span(
                            workspace=self.workspace,
                            artifact=artifact,
                            row=row,
                        )
                open_file.assert_not_called()


class HybridRetrieverContractTests(unittest.TestCase):
    @staticmethod
    def _row(name: str) -> dict[str, object]:
        return {
            "point_id": f"point-{name}",
            "span_id": f"span-{name}",
            "artifact_id": f"artifact-{name}",
            "revision_id": f"revision-{name}",
            "relative_path": f"plans/{name}.md",
            "content_sha256": f"content-{name}",
            "span_sha256": f"span-sha-{name}",
            "char_start": 0,
            "char_end": 5,
            "byte_start": 0,
            "byte_end": 5,
            "line_start": 1,
            "line_end": 1,
            "heading": name,
            "identifiers": "",
            "content": f"{name} content",
            "embedding_text": f"{name} content",
            "artifact_type": "plan",
            "authority_class": "working",
            "project": "test-project",
            "repository": None,
        }

    @platform_skips.requires_qdrant_stack
    def test_abstention_is_recomputed_after_source_verification(self) -> None:
        first = self._row("invalid")
        second = self._row("valid")
        lexical = SimpleNamespace(
            metadata={"generation": "generation-a", "profile_digest": "profile-a"},
            search=mock.Mock(return_value=[(first, -2.0), (second, -1.0)]),
            lookup=mock.Mock(return_value={}),
        )
        with tempfile.TemporaryDirectory() as root:
            retriever = retrieval.HybridRetriever(
                workspace=Path(root),
                catalog=Path(root) / "catalog.sqlite3",
                lexical=lexical,
            )
            retriever.current_artifacts = mock.Mock(
                return_value={
                    str(first["relative_path"]): SimpleNamespace(
                        revision_id=first["revision_id"]
                    ),
                    str(second["relative_path"]): SimpleNamespace(
                        revision_id=second["revision_id"]
                    ),
                }
            )
            client = mock.Mock()
            client.query_points.return_value = SimpleNamespace(points=[])

            def verify(
                *,
                workspace: Path,
                artifact: object,
                row: dict[str, object],
            ) -> dict[str, object]:
                del workspace, artifact
                if row["span_id"] == "span-invalid":
                    raise retrieval.RetrievalError("invalid source")
                return {
                    "content": "valid content",
                    "span_sha256": "span-sha-valid",
                    "span": {"byte_start": 0, "byte_end": 5},
                }

            with mock.patch.object(
                retrieval,
                "_verified_canonical_span",
                side_effect=verify,
            ):
                response = retriever.search(
                    client=client,
                    collection="exact",
                    query="ordinary words",
                    query_vector=[0.0] * retrieval.EMBEDDING_DIMENSIONS,
                    limit=1,
                    mode="hybrid-rerank",
                    reranker=lambda _query, _documents: [0.9, 0.8],
                    abstention_policy={
                        "exact_identifier_cross_min": 0.85,
                        "agreement_cross_min": 0.85,
                        "single_channel_cross_min": 0.85,
                        "cross_margin_min": 0.05,
                    },
                )

        self.assertTrue(response["abstained"])
        self.assertEqual(response["abstention_reason"], "below-calibrated-threshold")
        self.assertEqual(response["abstention_features"]["cross_score"], 0.8)
        self.assertEqual(response["results"], [])
        self.assertEqual(len(response["verification_failures"]), 1)

    @platform_skips.requires_qdrant_stack
    def test_nonfinite_vector_and_reranker_scores_fail_closed(self) -> None:
        row = self._row("candidate")
        lexical = SimpleNamespace(
            metadata={"generation": "generation-a", "profile_digest": "profile-a"},
            search=mock.Mock(return_value=[(row, -1.0)]),
            lookup=mock.Mock(return_value={str(row["point_id"]): row}),
        )
        with tempfile.TemporaryDirectory() as root:
            retriever = retrieval.HybridRetriever(
                workspace=Path(root),
                catalog=Path(root) / "catalog.sqlite3",
                lexical=lexical,
            )
            client = mock.Mock()
            client.query_points.return_value = SimpleNamespace(
                points=[
                    SimpleNamespace(
                        id=row["point_id"],
                        score=float("nan"),
                    )
                ]
            )
            with self.assertRaisesRegex(
                retrieval.RetrievalError,
                "vector search returned a non-finite",
            ):
                retriever.search(
                    client=client,
                    collection="exact",
                    query="candidate",
                    query_vector=[0.0] * retrieval.EMBEDDING_DIMENSIONS,
                    limit=1,
                    mode="vector",
                    reranker=None,
                )
            client.query_points.return_value = SimpleNamespace(points=[])
            with self.assertRaisesRegex(
                retrieval.RetrievalError,
                "reranker returned a non-finite",
            ):
                retriever.search(
                    client=client,
                    collection="exact",
                    query="candidate",
                    query_vector=[0.0] * retrieval.EMBEDDING_DIMENSIONS,
                    limit=1,
                    mode="hybrid-rerank",
                    reranker=lambda _query, _documents: [float("inf")],
                )


class LexicalIndexSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = self.root / "manifest.sqlite3"
        connection = sqlite3.connect(self.manifest)
        span_generation._create_schema(connection)
        connection.execute(f"PRAGMA user_version={retrieval.SCHEMA_VERSION}")
        for key, value in (
            ("generation", "generation-a"),
            ("profile_digest", "profile-a"),
            ("embedding_model_manifest_digest", "model-a"),
            ("span_manifest_digest", "span-set-a"),
            ("spans", 1),
            ("profile_id", "profile-id"),
            ("catalog_status", "complete"),
            ("catalog_artifacts", 1),
        ):
            connection.execute(
                "INSERT INTO metadata(key, value_json) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
        self._insert_span(connection, row_id=1, point_id="active", ready=1, current=1)
        connection.commit()
        connection.close()
        self.manifest.chmod(0o600)
        self.index = retrieval.LexicalIndex(
            self.manifest,
            generation="generation-a",
            profile_digest="profile-a",
            model_manifest_digest="model-a",
        )

    def tearDown(self) -> None:
        # LexicalIndex pins the manifest open on purpose: the held descriptor is
        # what stops the path being swapped between the identity check and the
        # read. Windows enforces that by refusing to unlink an open file, so a
        # teardown that does not close the index cannot remove its own tempdir.
        # Closing is the caller's job, here as in production.
        index = getattr(self, "index", None)
        if index is not None:
            index.close()
        self.temp.cleanup()

    @staticmethod
    def _insert_span(
        connection: sqlite3.Connection,
        *,
        row_id: int,
        point_id: str,
        ready: int,
        current: int,
    ) -> None:
        artifact_id = f"artifact-{row_id}"
        revision_id = f"revision-{row_id}"
        relative_path = f"plans/{point_id}.md"
        connection.execute(
            """
            INSERT INTO artifacts(
              artifact_id, revision_id, relative_path, content_sha256,
              byte_size, mtime_ns, artifact_type, authority_class, project,
              repository, source_scope, lifecycle_hints_json, representation,
              diagnostic, catalog_current
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                revision_id,
                relative_path,
                f"content-{row_id}",
                5,
                1,
                "plan",
                "working",
                "test-project",
                None,
                "workspace",
                '["active"]',
                "searchable",
                None,
                current,
            ),
        )
        connection.execute(
            """
            INSERT INTO spans(
              row_id, point_id, span_id, artifact_id, revision_id,
              relative_path, content_sha256, span_sha256, char_start,
              char_end, byte_start, byte_end, line_start, line_end, heading,
              identifiers, content, embedding_text, content_tokens,
              embedding_tokens, artifact_type, authority_class,
              lifecycle_hints_json, source_scope, repository, project,
              profile_id, profile_digest, collection_generation, ready,
              catalog_current
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                row_id,
                point_id,
                f"span-{row_id}",
                artifact_id,
                revision_id,
                relative_path,
                f"content-{row_id}",
                f"span-sha-{row_id}",
                0,
                5,
                0,
                5,
                1,
                1,
                "Alpha heading",
                "alpha repo:name",
                "alpha searchable content",
                "alpha searchable content",
                3,
                3,
                "plan",
                "working",
                '["active"]',
                "workspace",
                None,
                "test-project",
                "profile-id",
                "profile-a",
                "generation-a",
                ready,
                current,
            ),
        )

    def test_lexical_search_is_grammar_safe(self) -> None:
        results = self.index.search(
            'alpha" OR * NOT {beta} NEAR(gamma) column:value',
            limit=10,
        )

        self.assertEqual([row["point_id"] for row, _score in results], ["active"])

    def test_lookup_is_deduplicated_and_missing_ids_are_ignored(self) -> None:
        found = self.index.lookup(
            ["active", "active", "not-ready", "historical", "missing"]
        )
        self.assertEqual(list(found), ["active"])

    def test_mixed_or_incomplete_generation_fails_at_startup(self) -> None:
        self.index.close()
        connection = sqlite3.connect(self.manifest)
        self._insert_span(
            connection,
            row_id=2,
            point_id="not-ready",
            ready=0,
            current=1,
        )
        connection.execute(
            "UPDATE metadata SET value_json='2' WHERE key='spans'"
        )
        connection.execute(
            "UPDATE metadata SET value_json='2' WHERE key='catalog_artifacts'"
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(
            retrieval.RetrievalError,
            "complete, current, uniform generation",
        ):
            retrieval.LexicalIndex(
                self.manifest,
                generation="generation-a",
                profile_digest="profile-a",
                model_manifest_digest="model-a",
            )

    @platform_skips.requires_open_file_replacement
    def test_path_replacement_is_detected_while_pinned_inode_stays_open(self) -> None:
        replacement = self.root / "replacement.sqlite3"
        shutil.copyfile(self.manifest, replacement)
        replacement.chmod(0o600)
        os.replace(replacement, self.manifest)

        with self.assertRaisesRegex(
            retrieval.RetrievalError,
            "immutable identity changed",
        ):
            self.index.search("alpha")

    def test_generation_and_digest_mismatches_fail_closed(self) -> None:
        for arguments in (
            {"generation": "wrong"},
            {"generation": "generation-a", "profile_digest": "wrong"},
            {
                "generation": "generation-a",
                "model_manifest_digest": "wrong",
            },
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(
                    retrieval.RetrievalError,
                    "mismatch",
                ):
                    retrieval.LexicalIndex(self.manifest, **arguments)

    @staticmethod
    def _active_payload() -> dict[str, object]:
        return {
            "span_id": "span-1",
            "artifact_id": "artifact-1",
            "revision_id": "revision-1",
            "relative_path": "plans/active.md",
            "content_sha256": "content-1",
            "span_sha256": "span-sha-1",
            "byte_start": 0,
            "byte_end": 5,
            "line_start": 1,
            "line_end": 1,
            "artifact_type": "plan",
            "authority_class": "working",
            "lifecycle_hints": ["active"],
            "source_scope": "workspace",
            "repository": None,
            "project": "test-project",
            "profile_id": "profile-id",
            "profile_digest": "profile-a",
            "collection_generation": "generation-a",
            "span_manifest_digest": "span-set-a",
            "embedding_model_digest": "model-a",
            "ready": True,
            "catalog_current": True,
        }

    def test_full_qdrant_point_and_payload_set_matches_manifest(self) -> None:
        point = type(
            "Point",
            (),
            {
                "id": "active",
                "payload": self._active_payload(),
                "vector": [0.0] * retrieval.EMBEDDING_DIMENSIONS,
            },
        )()
        client = mock.Mock()
        client.scroll.return_value = ([point], None)

        result = retrieval.verify_qdrant_point_set(
            client=client,
            collection="exact",
            manifest=self.manifest,
        )

        self.assertEqual(result["points"], 1)
        self.assertEqual(result["span_manifest_digest"], "span-set-a")
        self.assertEqual(result["payload_contract"], "exact-manifest-row-v1")
        self.assertEqual(len(result["vector_set_sha256"]), 64)

    def test_same_cardinality_substitution_and_payload_drift_fail_closed(self) -> None:
        client = mock.Mock()
        substituted = type(
            "Point",
            (),
            {
                "id": "substitute",
                "payload": self._active_payload(),
                "vector": [0.0] * retrieval.EMBEDDING_DIMENSIONS,
            },
        )()
        client.scroll.return_value = ([substituted], None)
        with self.assertRaisesRegex(
            retrieval.RetrievalError,
            "absent from the exact manifest",
        ):
            retrieval.verify_qdrant_point_set(
                client=client,
                collection="exact",
                manifest=self.manifest,
            )

        drifted_payload = self._active_payload()
        drifted_payload["project"] = "other-project"
        drifted = type(
            "Point",
            (),
            {
                "id": "active",
                "payload": drifted_payload,
                "vector": [0.0] * retrieval.EMBEDDING_DIMENSIONS,
            },
        )()
        client.scroll.return_value = ([drifted], None)
        with self.assertRaisesRegex(
            retrieval.RetrievalError,
            "payload differs",
        ):
            retrieval.verify_qdrant_point_set(
                client=client,
                collection="exact",
                manifest=self.manifest,
            )

    def test_vector_shape_and_finiteness_are_part_of_point_set_proof(self) -> None:
        client = mock.Mock()
        for vector in (
            [0.0],
            [float("nan")] * retrieval.EMBEDDING_DIMENSIONS,
        ):
            with self.subTest(vector_length=len(vector)):
                point = SimpleNamespace(
                    id="active",
                    payload=self._active_payload(),
                    vector=vector,
                )
                client.scroll.return_value = ([point], None)
                with self.assertRaisesRegex(
                    retrieval.RetrievalError,
                    "vector",
                ):
                    retrieval.verify_qdrant_point_set(
                        client=client,
                        collection="exact",
                        manifest=self.manifest,
                    )

    @platform_skips.requires_qdrant_stack
    def test_vector_and_lexical_channels_receive_identical_filters(self) -> None:
        retriever = retrieval.HybridRetriever(
            workspace=self.root,
            catalog=self.manifest,
            lexical=self.index,
        )
        client = mock.Mock()
        client.query_points.return_value = SimpleNamespace(points=[])
        sentinel = object()
        filters = {
            "project": "test-project",
            "artifact_type": "plan",
            "authority_class": "working",
            "repository": "repo",
            "lifecycle_hint": "active",
        }
        with (
            mock.patch.object(
                retrieval,
                "_qdrant_filter",
                return_value=sentinel,
            ) as qdrant_filter,
            mock.patch.object(
                self.index,
                "search",
                return_value=[],
            ) as lexical_search,
        ):
            response = retriever.search(
                client=client,
                collection="exact",
                query="alpha",
                query_vector=[0.0] * retrieval.EMBEDDING_DIMENSIONS,
                limit=5,
                mode="hybrid",
                reranker=None,
                **filters,
            )

        self.assertTrue(response["abstained"])
        qdrant_filter.assert_called_once_with(
            mock.ANY,
            generation="generation-a",
            profile_digest="profile-a",
            **filters,
        )
        lexical_search.assert_called_once_with(
            "alpha",
            limit=retrieval.LEXICAL_CANDIDATES,
            **filters,
        )
        self.assertIs(
            client.query_points.call_args.kwargs["query_filter"],
            sentinel,
        )


if __name__ == "__main__":
    unittest.main()
