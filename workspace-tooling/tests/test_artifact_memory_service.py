from __future__ import annotations

import collections
import hashlib
import http.client
import json
import os
import re
import sqlite3
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(TEST_DIR))

import artifact_memory as memory  # noqa: E402
import artifact_memory_service as service  # noqa: E402
import artifact_retrieval as retrieval  # noqa: E402
import artifact_retrieval_eval as retrieval_eval  # noqa: E402
import artifact_retrieval_migrate as retrieval_migrate  # noqa: E402
import artifact_service_client as service_client  # noqa: E402
import artifact_span_generation as span_generation  # noqa: E402
from test_artifact_runtime import RuntimeFixture  # noqa: E402


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def model_manifest_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for candidate in sorted(path.rglob("*")):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(path).as_posix()
        file_digest = file_sha256(candidate)
        digest.update(
            f"{relative}\0{candidate.stat().st_size}\0{file_digest}\n".encode()
        )
    return digest.hexdigest()


def canonical_digest(document: object) -> str:
    return hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class FakeVector(list[float]):
    def tolist(self) -> list[float]:
        return list(self)


class FakeEmbeddingModel:
    loads = 0

    def __init__(self, *args: object, **kwargs: object):
        del args, kwargs
        type(self).loads += 1

    def query_embed(self, query: str):
        del query
        yield FakeVector([0.25, 0.5])

    def embed(self, documents: object, **kwargs: object):
        del kwargs
        return [FakeVector([0.25, 0.5]) for _ in documents]


class FakeRerankerModel:
    loads = 0

    def __init__(self, *args: object, **kwargs: object):
        del args, kwargs
        type(self).loads += 1

    def rerank(self, query: str, documents: object, **kwargs: object):
        del query, kwargs
        return [0.75 for _ in documents]


class FakeQdrantClient:
    collection_metadata: dict[str, object] = {}
    points = 1
    vector_size = 384
    vector_distance = "Cosine"
    instances: list["FakeQdrantClient"] = []

    def __init__(self, *args: object, **kwargs: object):
        del args, kwargs
        type(self).instances.append(self)
        self.query_calls: list[dict[str, object]] = []

    def collection_exists(self, collection: str) -> bool:
        del collection
        return True

    def info(self) -> object:
        return SimpleNamespace(version=retrieval.QDRANT_SERVER_VERSION)

    def get_collection(self, collection: str) -> object:
        del collection
        return SimpleNamespace(
            config=SimpleNamespace(
                metadata=dict(type(self).collection_metadata),
                params=SimpleNamespace(
                    vectors=SimpleNamespace(
                        size=type(self).vector_size,
                        distance=type(self).vector_distance,
                    )
                ),
            ),
            points_count=type(self).points,
        )

    def count(self, **kwargs: object) -> object:
        del kwargs
        return SimpleNamespace(count=type(self).points)

    def scroll(self, **kwargs: object) -> tuple[list[object], None]:
        del kwargs
        return ([], None)

    def close(self) -> None:
        return None


class ExactServiceFixture(RuntimeFixture):
    generation = "exact-g2"
    collection = "exact_spans_g2"
    span_digest = "1" * 64
    profile_digest = "2" * 64
    vector_leaf = hashlib.sha256(b"fixture-vector").digest()
    vector_digest = hashlib.sha256(vector_leaf).hexdigest()
    development_file_sha = "5" * 64
    holdout_file_sha = "6" * 64
    holdout_gold_digest = "8" * 64

    def __init__(self, root: Path):
        super().__init__(root)
        self._write_catalog()
        self.catalog_binding = service._catalog_binding(
            self.derived / "catalog.sqlite3"
        )
        self._write_model_snapshot(self.embedding_snapshot, b"embedding")
        self._write_model_snapshot(self.reranker_snapshot, b"reranker")
        self.embedding_digest = model_manifest_digest(self.embedding_snapshot)
        self.reranker_digest = model_manifest_digest(self.reranker_snapshot)
        self._write_manifest()
        self.manifest_sha = file_sha256(
            self.derived / "span-manifest.sqlite3"
        )
        policy_values = {
            "agreement_cross_min": 0.2,
            "cross_margin_min": 0.1,
            "exact_identifier_cross_min": 0.1,
            "single_channel_cross_min": 0.3,
        }
        self.policy_digest = canonical_digest(policy_values)
        migration_contract = retrieval_migrate._migration_contract(
            batch_size=1,
            inference_batch_size=1,
            threads=1,
        )
        migration_contract_digest = canonical_digest(migration_contract)
        migration_state = self.derived / "migration-state.sqlite3"
        with sqlite3.connect(migration_state) as connection:
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
                    "collection": self.collection,
                    "generation": self.generation,
                    "model_manifest_digest": self.embedding_digest,
                    "execution_contract_digest": migration_contract_digest,
                }.items(),
            )
            connection.execute(
                "INSERT INTO progress VALUES (1, 1, 1, '')"
            )
            connection.execute(
                "INSERT INTO vector_attestations VALUES (?, ?)",
                ("fixture-point", self.vector_leaf),
            )
        migration_state.chmod(0o600)
        migration = {
            "schema_version": 1,
            "status": "passed",
            "manifest": {
                "path": str(self.derived / "span-manifest.sqlite3"),
                "file_sha256": self.manifest_sha,
                "span_manifest_digest": self.span_digest,
                "profile_digest": self.profile_digest,
                "model_manifest_digest": self.embedding_digest,
                "spans": 1,
            },
            "qdrant": {
                "collection": self.collection,
                "generation": self.generation,
                "points": 1,
                "content_payload": False,
                "point_set": self.point_set(),
            },
            "embedding": {
                "model": retrieval.EMBEDDING_MODEL,
                "model_manifest_digest": self.embedding_digest,
                "local_files_only": True,
                "publication_batch_size": 1,
                "inference_batch_size": 1,
                "threads": 1,
            },
            "migration_contract": migration_contract,
            "migration_contract_digest": migration_contract_digest,
            "checkpoint": {
                "path": str(migration_state),
                "last_row_id": 1,
                "points": 1,
                "vector_attestations": 1,
                "vector_set_sha256": self.vector_digest,
            },
            "canonical_mutation": "disabled",
            "prior_generations_modified": False,
        }
        migration["evidence_digest"] = canonical_digest(migration)
        migration_path = self.write_json("migration-evidence.json", migration)
        self.system = {
            "generation": self.generation,
            "collection": self.collection,
            "span_manifest_digest": self.span_digest,
            "span_manifest_file_sha256": self.manifest_sha,
            "profile_digest": self.profile_digest,
            "embedding_model": "BAAI/bge-small-en-v1.5",
            "embedding_model_manifest_digest": self.embedding_digest,
            "reranker_model": "Xenova/ms-marco-MiniLM-L-6-v2",
            "reranker_model_manifest_digest": self.reranker_digest,
            "ranking_version": retrieval.RANKING_VERSION,
            "ranking_contract": retrieval.ranking_contract(),
            "gold_manifest_digest": "7" * 64,
            "gold_split_file_sha256": {
                "dev": self.development_file_sha,
                "holdout": self.holdout_file_sha,
            },
            "catalog_binding": dict(self.catalog_binding),
            "retrieval_scope": {
                "name": retrieval_eval.CURRENT_ONLY_SCOPE,
                "filters": dict(retrieval_eval.CURRENT_ONLY_FILTERS),
            },
            "material_sources": retrieval_eval._material_source_manifest(),
            "evaluation_runtime_contract": (
                retrieval_eval._evaluation_runtime_contract()
            ),
            "migration_evidence": {
                "path": str(migration_path),
                "file_sha256": file_sha256(migration_path),
                "evidence_digest": migration["evidence_digest"],
                "vector_set_sha256": self.vector_digest,
                "migration_contract_digest": migration_contract_digest,
            },
        }
        self.system_digest = canonical_digest(self.system)
        self.policy_values = policy_values
        self._write_release_chain()

    @staticmethod
    def _write_model_snapshot(path: Path, marker: bytes) -> None:
        model = path / "model.onnx"
        model.write_bytes(marker)
        model.chmod(0o600)
        tokenizer = path / "tokenizer.json"
        tokenizer.write_bytes(marker + b"-tokenizer")
        tokenizer.chmod(0o600)

    def _write_catalog(self) -> None:
        path = self.derived / "catalog.sqlite3"
        path.unlink(missing_ok=True)
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            PRAGMA user_version=3;
            CREATE TABLE scan_runs (
              run_id INTEGER PRIMARY KEY,
              finished_at TEXT
            );
            CREATE TABLE current_artifact_revisions (
              artifact_id TEXT NOT NULL,
              revision_id TEXT NOT NULL,
              relative_path TEXT NOT NULL,
              content_sha256 TEXT NOT NULL,
              byte_size INTEGER NOT NULL,
              mtime_ns INTEGER NOT NULL,
              artifact_type TEXT NOT NULL,
              authority_class TEXT NOT NULL,
              lifecycle_hints_json TEXT NOT NULL,
              source_scope TEXT NOT NULL,
              repository TEXT,
              project TEXT NOT NULL
            );
            INSERT INTO scan_runs VALUES (18, '2026-07-18T00:00:00Z');
            """
        )
        connection.execute(
            """
            INSERT INTO current_artifact_revisions VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                "artifact:test",
                "revision:" + "a" * 64,
                "plans/example.md",
                "b" * 64,
                10,
                1,
                "handoff",
                "current",
                "[]",
                "workspace",
                None,
                "workspace",
            ),
        )
        connection.commit()
        connection.close()
        path.chmod(0o600)

    def _write_manifest(self) -> None:
        path = self.derived / "span-manifest.sqlite3"
        path.unlink(missing_ok=True)
        connection = sqlite3.connect(path)
        span_generation._create_schema(connection)
        connection.execute(f"PRAGMA user_version={retrieval.SCHEMA_VERSION}")
        metadata = {
            "schema_version": 1,
            "generation": self.generation,
            "spans": 1,
            "catalog_run_id": self.catalog_binding["catalog_run_id"],
            "catalog_status": "complete",
            "catalog_artifacts": self.catalog_binding["catalog_artifacts"],
            "catalog_revision_set_sha256": self.catalog_binding[
                "catalog_revision_set_sha256"
            ],
            "profile_id": "canonical-bge384-heading-line-v1",
            "profile_digest": self.profile_digest,
            "span_manifest_digest": self.span_digest,
            "embedding_model": "BAAI/bge-small-en-v1.5",
            "embedding_model_manifest_digest": self.embedding_digest,
        }
        for key, value in metadata.items():
            connection.execute(
                "INSERT INTO metadata(key, value_json) VALUES (?, ?)",
                (key, json.dumps(value, sort_keys=True)),
            )
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
                "artifact:test",
                "revision:" + "a" * 64,
                "plans/example.md",
                "b" * 64,
                10,
                1,
                "handoff",
                "current",
                "workspace",
                None,
                "workspace",
                "[]",
                "searchable",
                None,
                1,
            ),
        )
        connection.execute(
            """
            INSERT INTO spans(
              point_id, span_id, artifact_id, revision_id, relative_path,
              content_sha256, span_sha256, char_start, char_end, byte_start,
              byte_end, line_start, line_end, heading, identifiers, content,
              embedding_text, content_tokens, embedding_tokens, artifact_type,
              authority_class, lifecycle_hints_json, source_scope, repository,
              project, profile_id, profile_digest, collection_generation,
              ready, catalog_current
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                "00000000-0000-5000-8000-000000000001",
                "span:" + "d" * 64,
                "artifact:test",
                "revision:" + "a" * 64,
                "plans/example.md",
                "b" * 64,
                "e" * 64,
                0,
                4,
                0,
                4,
                1,
                1,
                "",
                "",
                "test",
                "test",
                1,
                1,
                "handoff",
                "current",
                "[]",
                "workspace",
                None,
                "workspace",
                "canonical-bge384-heading-line-v1",
                self.profile_digest,
                self.generation,
                1,
                1,
            ),
        )
        connection.commit()
        connection.close()
        path.chmod(0o600)

    def write_json(self, name: str, document: object) -> Path:
        path = self.derived / name
        path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    @classmethod
    def point_set(cls) -> dict[str, object]:
        return {
            "points": 1,
            "span_manifest_digest": cls.span_digest,
            "payload_contract": "exact-manifest-row-v1",
            "vector_contract": (
                "sorted-point-id-sha256-float32le-v1/"
                f"{retrieval.EMBEDDING_DIMENSIONS}"
            ),
            "vector_set_sha256": cls.vector_digest,
        }

    def _write_evidence(
        self,
        split: str,
        policy: dict[str, object],
    ) -> tuple[str, Path]:
        evidence = {
            "schema_version": 1,
            "split": "dev" if split == "development" else "holdout",
            "status": "passed",
            "system": self.system,
            "system_digest": self.system_digest,
            "policy": policy,
            "gate": {"status": "passed"},
            "gold": {
                "digest": (
                    "4" * 64
                    if split == "development"
                    else self.holdout_gold_digest
                ),
            },
        }
        digest = canonical_digest(evidence)
        evidence["evidence_digest"] = digest
        path = self.write_json(f"{split}-evidence.json", evidence)
        return digest, path

    def _write_release_chain(self) -> None:
        policy_core: dict[str, object] = {
            "schema_version": 1,
            "ranking_version": retrieval.RANKING_VERSION,
            "policy": self.policy_values,
            "policy_digest": self.policy_digest,
            "system": self.system,
            "system_digest": self.system_digest,
            "development_gold_digest": "4" * 64,
            "development_selection_feasible": True,
        }
        self.development_digest, development_path = self._write_evidence(
            "development",
            policy_core,
        )
        release_policy = {
            **policy_core,
            "development_evidence": {
                "path": str(development_path),
                "file_sha256": file_sha256(development_path),
                "evidence_digest": self.development_digest,
            },
        }
        self.write_json("policy.json", release_policy)
        self.holdout_digest, holdout_path = self._write_evidence(
            "holdout",
            release_policy,
        )
        with mock.patch.object(
            retrieval_eval.artifact_runtime,
            "DEFAULT_DERIVED_ROOT",
            self.derived,
        ):
            ledger_path = retrieval_eval._holdout_ledger_path(
                gold_file_sha256=self.holdout_file_sha,
                generation=self.generation,
                span_manifest_digest=self.span_digest,
            )
        self.write_json(
            ledger_path.name,
            {
                "schema_version": 1,
                "state": "completed",
                "reserved_unix": 1.0,
                "completed_unix": 2.0,
                "gold_file_sha256": self.holdout_file_sha,
                "generation": self.generation,
                "span_manifest_digest": self.span_digest,
                "policy_digest": self.policy_digest,
                "system_digest": self.system_digest,
                "gold_digest": self.holdout_gold_digest,
                "evidence_digest": self.holdout_digest,
                "evidence_file_sha256": file_sha256(holdout_path),
            },
        )

    def exact_payload(self) -> dict[str, object]:
        payload = self.payload()
        exact = payload["retrieval"]
        assert isinstance(exact, dict)
        exact.update(
            {
                "collection": self.collection,
                "generation": self.generation,
                "manifest_sha256": self.manifest_sha,
                "span_manifest_digest": self.span_digest,
                "profile_digest": self.profile_digest,
                "embedding_model_manifest_digest": self.embedding_digest,
                "reranker_model_manifest_digest": self.reranker_digest,
                "ranking_version": retrieval.RANKING_VERSION,
                "policy_digest": self.policy_digest,
                "development_evidence_digest": self.development_digest,
                "holdout_evidence_digest": self.holdout_digest,
            }
        )
        return payload

    def collection_binding(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "generation": self.generation,
            "profile_id": "canonical-bge384-heading-line-v1",
            "profile_digest": self.profile_digest,
            "span_manifest_digest": self.span_digest,
            "manifest_file_sha256": self.manifest_sha,
            "embedding_model": "BAAI/bge-small-en-v1.5",
            "embedding_model_manifest_digest": self.embedding_digest,
            "content_payload": False,
            "current_only": True,
        }

    def rewrite_frozen_system(self, system: dict[str, object]) -> None:
        self.system = system
        self.system_digest = canonical_digest(system)
        self._write_release_chain()


class ExactServiceStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = ExactServiceFixture(Path(self.temp.name))
        FakeEmbeddingModel.loads = 0
        FakeRerankerModel.loads = 0
        FakeQdrantClient.instances = []
        FakeQdrantClient.points = 1
        FakeQdrantClient.vector_size = 384
        FakeQdrantClient.vector_distance = "Cosine"
        FakeQdrantClient.collection_metadata = self.fixture.collection_binding()
        self.legacy_loader = mock.Mock(return_value=mock.Mock())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def initialize(
        self,
        payload: dict[str, object] | None = None,
    ) -> service.ServiceState:
        config = self.fixture.write(payload or self.fixture.exact_payload())
        health = self.fixture.derived / "health.json"
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch(
                    "qdrant_client.QdrantClient",
                    FakeQdrantClient,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    service.artifact_runtime,
                    "DEFAULT_DERIVED_ROOT",
                    self.fixture.derived,
                )
            )
            stack.enter_context(
                mock.patch(
                    "fastembed.TextEmbedding",
                    FakeEmbeddingModel,
                )
            )
            stack.enter_context(
                mock.patch(
                    "fastembed.rerank.cross_encoder.TextCrossEncoder",
                    FakeRerankerModel,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    service.ingestion,
                    "create_text_embedder",
                    self.legacy_loader,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    service.artifact_retrieval,
                    "verify_qdrant_point_set",
                    return_value={
                        **self.fixture.point_set(),
                    },
                )
            )
            if hasattr(service, "QdrantClient"):
                stack.enter_context(
                    mock.patch.object(
                        service,
                        "QdrantClient",
                        FakeQdrantClient,
                    )
                )
            return service.ServiceState(config, health)

    def test_exact_startup_loads_each_resident_model_once(self) -> None:
        state = self.initialize()
        try:
            self.assertEqual(FakeEmbeddingModel.loads, 1)
            self.assertEqual(FakeRerankerModel.loads, 1)
            self.assertEqual(
                len(FakeQdrantClient.instances),
                1,
                "read-only service must construct no admin Qdrant client",
            )
            self.legacy_loader.assert_not_called()
            state.exact.retriever.search = mock.Mock(
                return_value={"results": [], "abstained": True}
            )
            state.search({"query": "first"})
            state.search({"query": "second"})
            self.assertEqual(FakeEmbeddingModel.loads, 1)
            self.assertEqual(FakeRerankerModel.loads, 1)
            self.legacy_loader.assert_not_called()
            operations = state.operational_summary()
            self.assertEqual(operations["retrieval"]["searches"], 2)
            self.assertEqual(operations["retrieval"]["abstentions"], 2)
            self.assertEqual(operations["retrieval"]["underfilled"], 0)
            self.assertEqual(
                operations["retrieval"]["hash_or_source_conflicts"],
                0,
            )
            self.assertIsInstance(operations["queues"]["embedding"], dict)
            self.assertIsInstance(operations["queues"]["reranker"], dict)
        finally:
            state.close()

    def legacy_payload(self, **overrides: object) -> dict[str, object]:
        payload = self.fixture.payload(active="legacy-vector-v1")
        payload.pop("retrieval")
        payload.update(overrides)
        return payload

    def test_server_backend_never_opens_the_rollback_embedded_store(self) -> None:
        """The embedded store is retained read-only for rollback only.

        Opening it under the server backend would put a third process on the
        local-mode lock the server migration exists to avoid.
        """
        state = self.initialize(self.legacy_payload())
        try:
            self.assertIsNone(state._embedded_client)
            state.readiness()
            with ExitStack() as stack:
                # The fixture catalog has no scan_runs table; the store
                # selection under test is independent of catalog reporting.
                stack.enter_context(
                    mock.patch.object(
                        service.artifact_memory,
                        "_catalog_status",
                        return_value={"available": True},
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        service.artifact_memory,
                        "_graphiti_status",
                        return_value={"available": None},
                    )
                )
                state.status({})
            client, local_path, url = state.active_client()

            self.assertIsNone(state._embedded_client)
            self.assertIs(client, state.read_client)
            self.assertIsNone(local_path)
            self.assertEqual(url, state.runtime.qdrant_url)
        finally:
            state.close()

    def test_embedded_backend_still_opens_the_store_for_rollback_rehearsal(
        self,
    ) -> None:
        state = self.initialize(self.legacy_payload(active_backend="embedded"))
        try:
            self.assertIsNone(state._embedded_client)
            client, local_path, url = state.active_client()

            self.assertIsNotNone(state._embedded_client)
            self.assertIs(client, state._embedded_client)
            self.assertEqual(local_path, state.runtime.embedded_path)
            self.assertIsNone(url)
        finally:
            state.close()

    def test_rollback_return_to_server_releases_the_embedded_store(self) -> None:
        state = self.initialize(self.legacy_payload(active_backend="embedded"))
        try:
            state.active_client()
            self.assertIsNotNone(state._embedded_client)

            self.fixture.write(self.legacy_payload())
            state.refresh_runtime()

            self.assertIsNone(state._embedded_client)
            self.assertEqual(state.runtime.active_backend, "server")
        finally:
            state.close()

    def test_manifest_collection_model_and_policy_mismatches_fail_closed(self) -> None:
        cases = (
            ("manifest_sha256", "9" * 64, None),
            ("span_manifest_digest", "6" * 64, None),
            ("collection", None, {"generation": "wrong"}),
            ("embedding_model_manifest_digest", "8" * 64, None),
            ("policy_digest", "7" * 64, None),
        )
        for field, configured, collection_update in cases:
            with self.subTest(field=field):
                payload = self.fixture.exact_payload()
                exact = payload["retrieval"]
                assert isinstance(exact, dict)
                if configured is not None:
                    exact[field] = configured
                FakeQdrantClient.collection_metadata = (
                    self.fixture.collection_binding()
                )
                if collection_update:
                    FakeQdrantClient.collection_metadata.update(collection_update)
                with self.assertRaises(
                    (
                        service.ServiceError,
                        retrieval.RetrievalError,
                        ValueError,
                    )
                ):
                    self.initialize(payload)

    def test_partial_collection_fails_closed_even_with_matching_metadata(self) -> None:
        FakeQdrantClient.collection_metadata = self.fixture.collection_binding()
        FakeQdrantClient.points = 0

        with self.assertRaisesRegex(
            service.ServiceError,
            "point|count|partial",
        ):
            self.initialize()

    def test_readiness_and_health_file_degrade_on_qdrant_point_drift(self) -> None:
        state = self.initialize()
        try:
            FakeQdrantClient.points = 2
            readiness = state.readiness()
            self.assertFalse(readiness["available"])
            self.assertEqual(readiness["status"], "degraded")
            self.assertEqual(readiness["collection"], self.fixture.collection)
            self.assertEqual(readiness["generation"], self.fixture.generation)
            self.assertEqual(readiness["error"], "ServiceError")

            state.write_health("healthy", force=True)
            health = json.loads(
                state.health_path.read_text(encoding="utf-8")
            )
            self.assertEqual(health["status"], "degraded")
            self.assertFalse(health["readiness"]["available"])
            self.assertGreaterEqual(
                health["operational_metrics"]["health"][
                    "degraded_readiness_probes"
                ],
                2,
            )
        finally:
            state.close()

    def test_readiness_degrades_on_qdrant_release_metadata_drift(self) -> None:
        state = self.initialize()
        try:
            FakeQdrantClient.collection_metadata = (
                self.fixture.collection_binding()
            )
            FakeQdrantClient.collection_metadata["generation"] = "wrong"

            readiness = state.readiness()

            self.assertFalse(readiness["available"])
            self.assertEqual(readiness["status"], "degraded")
            self.assertEqual(readiness["error"], "ServiceError")
        finally:
            state.close()

    def test_collection_vector_space_must_match_the_evaluated_model(self) -> None:
        cases = (("size", 128), ("distance", "Dot"))
        for field, value in cases:
            with self.subTest(field=field):
                FakeQdrantClient.vector_size = 384
                FakeQdrantClient.vector_distance = "Cosine"
                if field == "size":
                    FakeQdrantClient.vector_size = int(value)
                else:
                    FakeQdrantClient.vector_distance = str(value)
                with self.assertRaisesRegex(
                    service.ServiceError,
                    "vector|distance|collection",
                ):
                    self.initialize()

    def test_retrieval_selector_change_requires_a_service_restart(self) -> None:
        state = self.initialize()
        try:
            legacy = self.fixture.payload(active="legacy-vector-v1")
            legacy.pop("retrieval")
            self.fixture.write(legacy)
            with self.assertRaisesRegex(
                service.ServiceError,
                "restart required.*active_retrieval",
            ):
                state.refresh_runtime()
            self.assertEqual(
                state.runtime.active_retrieval,
                "exact-hybrid-v2",
            )
        finally:
            state.close()

    def test_exact_backend_change_is_rejected_while_service_is_running(self) -> None:
        state = self.initialize()
        try:
            payload = self.fixture.exact_payload()
            payload["active_backend"] = "embedded"
            self.fixture.write(payload)
            with self.assertRaisesRegex(
                ValueError,
                "active_backend=server",
            ):
                state.refresh_runtime()
            self.assertEqual(state.runtime.active_backend, "server")
        finally:
            state.close()

    def test_service_socket_path_change_requires_a_service_restart(self) -> None:
        state = self.initialize()
        try:
            payload = self.fixture.exact_payload()
            service_config = payload["service"]
            assert isinstance(service_config, dict)
            service_config["socket_path"] = str(
                self.fixture.derived / "replacement-artifact-memory.sock"
            )
            self.fixture.write(payload)
            with self.assertRaisesRegex(
                service.ServiceError,
                "restart required.*service_socket_path",
            ):
                state.refresh_runtime()
        finally:
            state.close()

    def test_catalog_revision_set_mismatch_fails_startup(self) -> None:
        catalog = self.fixture.derived / "catalog.sqlite3"
        with sqlite3.connect(catalog) as connection:
            connection.execute(
                """
                UPDATE current_artifact_revisions
                SET content_sha256=?
                """,
                ("c" * 64,),
            )
            connection.commit()
        with self.assertRaisesRegex(
            service.ServiceError,
            "catalog revision set",
        ):
            self.initialize()

    def test_catalog_drift_after_startup_blocks_exact_search(self) -> None:
        state = self.initialize()
        try:
            assert state.exact is not None
            state.exact.retriever.search = mock.Mock(
                return_value={"results": [], "abstained": True}
            )
            catalog = self.fixture.derived / "catalog.sqlite3"
            with sqlite3.connect(catalog) as connection:
                connection.execute(
                    """
                    UPDATE current_artifact_revisions
                    SET revision_id=?
                    """,
                    ("revision:" + "c" * 64,),
                )
                connection.commit()
            with self.assertRaisesRegex(
                service.ServiceError,
                "generation is stale",
            ):
                state.search({"query": "must not use a stale generation"})
            state.exact.retriever.search.assert_not_called()
            self.assertFalse(state.exact.freshness()["fresh"])
            self.assertEqual(
                state.retrieval_outcomes["catalog_or_manifest_conflicts"],
                1,
            )
        finally:
            state.close()

    def test_manifest_path_replacement_requires_a_service_restart(self) -> None:
        state = self.initialize()
        try:
            assert state.exact is not None
            state.exact.retriever.search = mock.Mock(
                return_value={"results": [], "abstained": True}
            )
            manifest = self.fixture.derived / "span-manifest.sqlite3"
            replacement = self.fixture.derived / ".replacement-manifest"
            replacement.write_bytes(manifest.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(manifest)

            with self.assertRaisesRegex(
                service.ServiceError,
                "generation is stale",
            ):
                state.search({"query": "must not use a replaced manifest"})
            state.exact.retriever.search.assert_not_called()
            freshness = state.exact.freshness()
            self.assertFalse(freshness["fresh"])
            self.assertFalse(freshness["manifest_file_current"])
        finally:
            state.close()

    def test_exact_search_rejects_a_hot_backend_configuration_change(self) -> None:
        state = self.initialize()
        try:
            assert state.exact is not None
            state.exact.retriever.search = mock.Mock()
            payload = self.fixture.exact_payload()
            payload["active_backend"] = "embedded"
            self.fixture.write(payload)

            with self.assertRaisesRegex(
                ValueError,
                "active_backend=server",
            ):
                state.search({"query": "must fail closed"})
            state.exact.retriever.search.assert_not_called()
            self.assertEqual(state.runtime.active_backend, "server")
        finally:
            state.close()

    def test_frozen_ranking_constants_must_match_the_running_ranker(self) -> None:
        altered = json.loads(json.dumps(self.fixture.system))
        altered["ranking_contract"]["rrf_k"] = 999.0
        self.fixture.rewrite_frozen_system(altered)

        with self.assertRaisesRegex(
            service.ServiceError,
            "ranking|system",
        ):
            self.initialize(self.fixture.exact_payload())

    def test_model_name_cannot_change_while_reusing_a_snapshot_digest(self) -> None:
        payload = self.fixture.exact_payload()
        exact = payload["retrieval"]
        assert isinstance(exact, dict)
        exact["embedding_model"] = "not-the-evaluated-model"

        with self.assertRaisesRegex(
            service.ServiceError,
            "model|system",
        ):
            self.initialize(payload)


def manual_state(
    *,
    active_retrieval: str = "exact-hybrid-v2",
) -> tuple[service.ServiceState, mock.Mock, mock.Mock]:
    state = service.ServiceState.__new__(service.ServiceState)
    exact = SimpleNamespace(
        collection="exact_spans_g2",
        generation="exact-g2",
        manifest_sha256="a" * 64,
        span_manifest_digest="b" * 64,
        profile_digest="c" * 64,
        embedding_model_manifest_digest="d" * 64,
        reranker_model_manifest_digest="e" * 64,
        ranking_version=retrieval.RANKING_VERSION,
        policy_digest="f" * 64,
        development_evidence_digest="1" * 64,
        holdout_evidence_digest="2" * 64,
    )
    state.runtime = SimpleNamespace(
        active_retrieval=active_retrieval,
        retrieval=exact if active_retrieval == "exact-hybrid-v2" else None,
        qdrant_collection="legacy_chunks_g1",
        qdrant_generation="legacy-g1",
        active_backend="server",
        build_manifest=Path("/nonexistent"),
        receipt_root=Path("/nonexistent"),
        consumer_state=Path("/nonexistent"),
        catalog=Path("/nonexistent"),
    )
    state.exact = None
    state.refresh_runtime = mock.Mock()
    state.read_client = mock.Mock()
    state.started = 0.0
    state.metric_summary = mock.Mock(return_value={})
    state.operational_summary = mock.Mock(return_value={})
    state.record_retrieval_result = mock.Mock()
    state.record_integrity_conflict = mock.Mock()
    retriever = mock.Mock()
    retriever.search.return_value = {
        "results": [],
        "abstained": True,
        "abstention_reason": "no-candidate",
    }
    embedder = mock.Mock()
    embedder.query_embed.return_value = iter([FakeVector([0.1, 0.2])])
    embedder.embed_query.return_value = FakeVector([0.1, 0.2])
    reranker = mock.Mock(return_value=[])
    policy = {
        "exact_identifier_cross_min": 0.1,
        "agreement_cross_min": 0.2,
        "single_channel_cross_min": 0.3,
        "cross_margin_min": 0.1,
    }
    if active_retrieval == "exact-hybrid-v2":
        state.exact = SimpleNamespace(
            config=exact,
            retriever=retriever,
            embedder=embedder,
            reranker=reranker,
            policy=policy,
            policy_digest=exact.policy_digest,
            system_digest="3" * 64,
            point_set={
                "points": 1,
                "span_manifest_digest": "b" * 64,
                "payload_contract": "exact-manifest-row-v1",
            },
            assert_fresh=mock.Mock(),
            qdrant_readiness=mock.Mock(return_value=1),
            freshness=mock.Mock(
                return_value={
                    "fresh": True,
                    "manifest": {
                        "catalog_run_id": 18,
                        "catalog_artifacts": 1,
                        "catalog_revision_set_sha256": "4" * 64,
                    },
                    "current": {
                        "catalog_run_id": 18,
                        "catalog_artifacts": 1,
                        "catalog_revision_set_sha256": "4" * 64,
                    },
                    "manifest_file_current": True,
                    "error": None,
                }
            ),
        )
    return state, retriever, embedder


class ExactServiceRouteTests(unittest.TestCase):
    def test_search_delegates_to_hybrid_rerank_and_preserves_history_gate(self) -> None:
        state, retriever, embedder = manual_state()

        result = state.search(
            {
                "query": "provider neutral receipt",
                "limit": 5,
                "project": "workspace",
                "include_history": False,
            }
        )

        self.assertTrue(result["abstained"])
        call = retriever.search.call_args
        self.assertIsNotNone(call)
        assert call is not None
        self.assertEqual(call.kwargs["collection"], "exact_spans_g2")
        self.assertEqual(call.kwargs["mode"], "hybrid-rerank")
        self.assertEqual(call.kwargs["limit"], 5)
        self.assertEqual(call.kwargs["project"], "workspace")
        self.assertIsNotNone(call.kwargs["reranker"])
        self.assertIsNotNone(call.kwargs["abstention_policy"])

        retriever.reset_mock()
        embedder.reset_mock()
        with self.assertRaisesRegex(
            memory.MemoryReadError,
            "historical artifact snippets are disabled",
        ):
            state.search(
                {
                    "query": "old revision",
                    "include_history": True,
                }
            )
        retriever.search.assert_not_called()
        self.assertFalse(embedder.query_embed.called)
        self.assertFalse(embedder.embed_query.called)

    def test_exact_search_never_requests_or_reads_qdrant_payload_content(self) -> None:
        class PayloadRejectingPoint:
            id = "not-used"
            score = 0.25

            @property
            def payload(self) -> object:
                raise AssertionError("Qdrant payload must not be read")

        class ReadClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def query_points(self, **kwargs: object) -> object:
                self.calls.append(dict(kwargs))
                return SimpleNamespace(points=[PayloadRejectingPoint()])

        class EmptyLexical:
            metadata = {
                "generation": "exact-g2",
                "profile_digest": "c" * 64,
            }

            def lookup(self, point_ids: object) -> dict[str, object]:
                # Consume the IDs so a payload-reading iterator would still fail.
                list(point_ids)
                return {}

            def search(self, *args: object, **kwargs: object) -> list[object]:
                del args, kwargs
                return []

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            catalog = root / "catalog.sqlite3"
            catalog.write_bytes(b"unused")
            state, _retriever, _embedder = manual_state()
            client = ReadClient()
            state.read_client = client
            assert state.exact is not None
            state.exact.retriever = retrieval.HybridRetriever(
                workspace=workspace,
                catalog=catalog,
                lexical=EmptyLexical(),
            )

            result = state.search({"query": "payloadless candidate"})

        self.assertTrue(result["abstained"])
        self.assertEqual(len(client.calls), 1)
        self.assertIs(client.calls[0]["with_payload"], False)

    def test_listener_rejects_ingest_and_reconcile_in_every_mode(self) -> None:
        state, _retriever, _embedder = manual_state()
        for active in ("exact-hybrid-v2", "legacy-vector-v1"):
            with self.subTest(active=active):
                state.runtime.active_retrieval = active
                with self.assertRaisesRegex(
                    service.ServiceError,
                    "listener is read-only",
                ):
                    state.ingest({"outbox_name": "skill-event-example"})
                with self.assertRaisesRegex(
                    service.ServiceError,
                    "listener is read-only",
                ):
                    state.reconcile({})

    def test_legacy_selector_uses_the_retained_v1_path(self) -> None:
        state, _retriever, embedder = manual_state(
            active_retrieval="legacy-vector-v1"
        )
        state.embedder = embedder
        state.active_client = mock.Mock(
            return_value=(state.read_client, None, "http://127.0.0.1:6333")
        )
        state.current_revisions = mock.Mock(return_value={"revision-1"})
        state.runtime.workspace = Path("/tmp")
        state.runtime.catalog = Path("/tmp/catalog.sqlite3")
        state.runtime.qdrant_url = "http://127.0.0.1:6333"
        state.runtime.qdrant_read_key = mock.Mock(return_value="read-key")
        with mock.patch.object(
            service.ingestion,
            "qdrant_search",
            return_value={"results": []},
        ) as legacy:
            result = state.search({"query": "rollback route"})

        self.assertEqual(result["retrieval_path"], "resident-vector-v1")
        self.assertEqual(result["generation"], "legacy-g1")
        self.assertEqual(
            legacy.call_args.kwargs["collection"],
            "legacy_chunks_g1",
        )

    def test_status_reports_the_complete_active_generation_identity(self) -> None:
        state, _retriever, _embedder = manual_state()
        state.read_client.count.return_value = SimpleNamespace(count=1)
        with mock.patch.object(
            service.artifact_memory,
            "_catalog_status",
            return_value={"available": True},
        ), mock.patch.object(
            service.artifact_memory,
            "_graphiti_status",
            return_value={"available": False},
        ), mock.patch.object(
            service.artifact_memory,
            "_receipt_count",
            return_value=0,
        ), mock.patch.object(
            service.artifact_memory,
            "_consumer_status",
            return_value={"available": True},
        ), mock.patch.object(service.time, "time", return_value=10.0):
            result = state.status()

        self.assertEqual(
            result["retrieval_release"],
            {
                "collection": "exact_spans_g2",
                "generation": "exact-g2",
                "manifest_sha256": "a" * 64,
                "span_manifest_digest": "b" * 64,
                "profile_digest": "c" * 64,
                "embedding_model_manifest_digest": "d" * 64,
                "reranker_model_manifest_digest": "e" * 64,
                "ranking_version": retrieval.RANKING_VERSION,
                "policy_digest": "f" * 64,
                "development_evidence_digest": "1" * 64,
                "holdout_evidence_digest": "2" * 64,
                "system_digest": "3" * 64,
                "point_set": {
                    "points": 1,
                    "span_manifest_digest": "b" * 64,
                    "payload_contract": "exact-manifest-row-v1",
                },
                "catalog_freshness": state.exact.freshness.return_value,
                "incremental_publication": "disabled-fail-closed",
            },
        )
        self.assertEqual(
            result["read_controls"]["historical_snippets"],
            "disabled",
        )

    def test_metrics_count_http_errors_and_retrieval_outcomes(self) -> None:
        state = service.ServiceState.__new__(service.ServiceState)
        state.metrics_lock = threading.Lock()
        state.requests = collections.Counter()
        state.errors = collections.Counter()
        state.http_statuses = collections.Counter()
        state.retrieval_outcomes = collections.Counter()
        state.health_outcomes = collections.Counter()
        state.latencies = collections.defaultdict(
            lambda: collections.deque(maxlen=2000)
        )
        state.inflight_requests = 0
        state.maximum_inflight_requests = 0
        state.concurrency_rejections = 0
        state.exact = None
        state.embedder = None

        for status_code in (400, 401, 404, 429, 500, 503):
            state.observe("/v1/search", 1.0, status_code)
        state.record_retrieval_result(
            {"results": [], "abstained": True},
            8,
        )
        state.record_retrieval_result(
            {"results": [{"id": "one"}], "abstained": False},
            8,
        )
        state.record_integrity_conflict("hash_or_source_conflicts")
        state.record_readiness(False)

        result = state.operational_summary()
        self.assertEqual(
            result["http_statuses"],
            {
                "400": 1,
                "401": 1,
                "404": 1,
                "429": 1,
                "500": 1,
                "503": 1,
            },
        )
        self.assertEqual(result["retrieval"]["searches"], 2)
        self.assertEqual(result["retrieval"]["abstentions"], 1)
        self.assertEqual(result["retrieval"]["underfilled"], 1)
        self.assertEqual(
            result["retrieval"]["hash_or_source_conflicts"],
            1,
        )
        self.assertEqual(
            result["health"]["degraded_readiness_probes"],
            1,
        )


class HTTPStubState:
    def __init__(
        self,
        *,
        acquire: bool = True,
        available: bool = True,
    ):
        self.acquire = acquire
        self.available = available
        self.acquires = 0
        self.releases = 0
        self.observations: list[tuple[str, int]] = []
        self.health_writes: list[str] = []
        self.handler_calls: list[str] = []

    def try_acquire_request_slot(self) -> bool:
        self.acquires += 1
        return self.acquire

    def release_request_slot(self) -> None:
        self.releases += 1

    def observe(
        self,
        route: str,
        duration_ms: float,
        status_code: int,
    ) -> None:
        del duration_ms
        self.observations.append((route, status_code))

    def write_health(self, status: str, error: str | None = None) -> None:
        del error
        self.health_writes.append(status)

    def readiness(self) -> dict[str, object]:
        return {
            "available": self.available,
            "status": "healthy" if self.available else "degraded",
            "active_backend": "server",
            "active_retrieval": "exact-hybrid-v2",
            "collection": "exact_spans_g2",
            "generation": "exact-g2",
            "catalog_freshness": {
                "fresh": self.available,
            },
        }

    def status(self, body: dict[str, object]) -> dict[str, object]:
        del body
        self.handler_calls.append("status")
        return {"service": {"available": self.available}}

    def search(self, body: dict[str, object]) -> dict[str, object]:
        del body
        self.handler_calls.append("search")
        return {"results": []}

    def get(self, body: dict[str, object]) -> dict[str, object]:
        del body
        self.handler_calls.append("get")
        return {"content": ""}

    def facts(self, body: dict[str, object]) -> dict[str, object]:
        del body
        self.handler_calls.append("facts")
        return {"facts": []}


@contextmanager
def running_unix_http_service(state: HTTPStubState):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        socket_path = root / "artifact-memory.sock"
        server = service.ArtifactUnixHTTPServer(socket_path, state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield socket_path
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path):
        super().__init__("localhost", timeout=2)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(str(self.socket_path))
        except BaseException:
            connection.close()
            raise
        self.sock = connection


def http_exchange(
    socket_path: Path,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object], dict[str, str]]:
    """Like http_request, but also returns the response headers."""
    connection = UnixHTTPConnection(socket_path)
    try:
        connection.request(
            method,
            path,
            body=body,
            headers=headers or {},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        return response.status, payload, dict(response.getheaders())
    finally:
        connection.close()


def http_request(
    socket_path: Path,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    status_code, payload, _ = http_exchange(
        socket_path,
        method,
        path,
        body=body,
        headers=headers,
    )
    return status_code, payload


class ArtifactUnixHTTPHandlerTests(unittest.TestCase):
    def test_degraded_healthz_is_qdrant_aware_and_returns_503(self) -> None:
        state = HTTPStubState(available=False)
        with running_unix_http_service(state) as socket_path:
            status_code, payload = http_request(socket_path, "GET", "/healthz")

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(payload["catalog_fresh"])
        self.assertEqual(payload["collection"], "exact_spans_g2")
        self.assertEqual(state.observations, [("/healthz", 503)])
        self.assertEqual(state.acquires, 1)
        self.assertEqual(state.releases, 1)

    def test_request_slot_is_rejected_before_any_body_read(self) -> None:
        state = HTTPStubState(acquire=False)
        with running_unix_http_service(state) as socket_path:
            status_code, payload = http_request(
                socket_path,
                "POST",
                "/v1/search",
                body=b"",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(service.MAX_REQUEST_BYTES),
                },
            )

        self.assertEqual(status_code, 429)
        self.assertIn("concurrency", str(payload["error"]))
        self.assertEqual(state.handler_calls, [])
        self.assertEqual(state.releases, 0)
        self.assertEqual(state.observations, [("/v1/search", 429)])

    def test_listener_has_no_internal_mutation_routes(self) -> None:
        state = HTTPStubState()
        body = b'{"outbox_name":"skill-event-example"}'
        with running_unix_http_service(state) as socket_path:
            status_code, payload = http_request(
                socket_path,
                "POST",
                "/v1/internal/ingest",
                body=body,
                headers={
                    "Content-Type": "application/json",
                },
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"], "not found")
        self.assertEqual(state.handler_calls, [])
        self.assertEqual(state.observations, [("__unknown__", 404)])

    def test_uds_transport_does_not_require_a_bearer_token(self) -> None:
        state = HTTPStubState()
        with running_unix_http_service(state) as socket_path:
            status_code, payload = http_request(
                socket_path,
                "POST",
                "/v1/status",
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["service"]["available"])
        self.assertEqual(state.handler_calls, ["status"])
        self.assertEqual(state.observations, [("/v1/status", 200)])

    def test_python_client_and_service_share_the_uds_contract(self) -> None:
        state = HTTPStubState()
        with running_unix_http_service(state) as socket_path:
            payload = service_client.post_json(
                socket_path=socket_path,
                route="/v1/status",
                payload={},
            )

        self.assertEqual(payload, {"service": {"available": True}})
        self.assertEqual(state.handler_calls, ["status"])

    def test_protocol_level_5xx_response_is_counted(self) -> None:
        state = HTTPStubState()
        with running_unix_http_service(state) as socket_path:
            status_code, payload = http_request(
                socket_path,
                "PUT",
                "/v1/search",
            )

        self.assertEqual(status_code, 501)
        self.assertEqual(payload["error"], "not implemented")
        self.assertEqual(state.observations, [("/v1/search", 501)])
        self.assertEqual(state.health_writes, ["degraded"])


class ArtifactProtocolVersionTests(unittest.TestCase):
    """Wire-protocol fencing between adapters and the resident service (F-09).

    A provider MCP process keeps its loaded adapter until the session restarts,
    so an upgraded service must refuse a stale client explicitly rather than
    serving it a shape it cannot parse.
    """

    def post(
        self,
        socket_path: Path,
        *,
        protocol: str | None,
        route: str = "/v1/status",
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        headers = {"Content-Type": "application/json"}
        if protocol is not None:
            headers[service.PROTOCOL_HEADER] = protocol
        return http_exchange(
            socket_path,
            "POST",
            route,
            body=b"{}",
            headers=headers,
        )

    def test_current_protocol_is_served_and_identity_is_announced(self) -> None:
        state = HTTPStubState()
        with running_unix_http_service(state) as socket_path:
            status_code, payload, headers = self.post(
                socket_path,
                protocol=str(service.PROTOCOL_VERSION),
            )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["service"]["available"])
        self.assertEqual(state.handler_calls, ["status"])
        self.assertEqual(
            headers[service.PROTOCOL_HEADER],
            str(service.PROTOCOL_VERSION),
        )
        self.assertEqual(
            headers[service.SCHEMA_HEADER],
            str(service.SCHEMA_VERSION),
        )
        self.assertEqual(headers[service.BUILD_HEADER], service.SERVICE_BUILD)
        self.assertNotEqual(service.SERVICE_BUILD, "unknown")

    def test_versionless_client_is_served_during_rollout(self) -> None:
        """The shipped adapter and client send no header yet; both must work."""
        state = HTTPStubState()
        with running_unix_http_service(state) as socket_path:
            status_code, _, headers = self.post(socket_path, protocol=None)

        self.assertEqual(status_code, 200)
        self.assertEqual(state.handler_calls, ["status"])
        self.assertEqual(
            headers[service.PROTOCOL_HEADER],
            str(service.PROTOCOL_VERSION),
        )

    def test_stale_adapter_against_upgraded_service_is_fenced(self) -> None:
        """Falsification #17: old adapter vs new service on every route."""
        for route in sorted(service.PUBLIC_ROUTES):
            with self.subTest(route=route):
                state = HTTPStubState()
                with mock.patch.object(service, "PROTOCOL_VERSION", 2):
                    with running_unix_http_service(state) as socket_path:
                        status_code, payload, headers = self.post(
                            socket_path,
                            protocol="1",
                            route=route,
                        )

                self.assertEqual(status_code, 426)
                self.assertEqual(payload["service_protocol_version"], 2)
                self.assertEqual(payload["action"], "restart-session")
                self.assertEqual(payload["service_build"], service.SERVICE_BUILD)
                self.assertIn("restart the session", str(payload["error"]))
                self.assertEqual(headers[service.PROTOCOL_HEADER], "2")
                self.assertEqual(
                    state.handler_calls,
                    [],
                    "a fenced client must never reach a route handler",
                )
                self.assertEqual(state.observations, [(route, 426)])
                self.assertEqual(
                    state.acquires,
                    0,
                    "fencing must precede slot acquisition and body dispatch",
                )
                self.assertNotIn("degraded", state.health_writes)

    def test_fenced_client_with_a_large_body_still_receives_the_426(self) -> None:
        """The rejection must be delivered, not lost to a broken pipe.

        Replying before draining the in-flight body raced the peer's write and
        surfaced as a transport error, which an adapter reports as "service
        unavailable" -- burying the restart instruction this fence exists for.
        """
        state = HTTPStubState()
        body = json.dumps({"query": "x" * 400_000}).encode("utf-8")
        with mock.patch.object(service, "PROTOCOL_VERSION", 2):
            with running_unix_http_service(state) as socket_path:
                status_code, payload, _ = http_exchange(
                    socket_path,
                    "POST",
                    "/v1/search",
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        service.PROTOCOL_HEADER: "1",
                    },
                )

        self.assertEqual(status_code, 426)
        self.assertIn("restart the session", str(payload["error"]))
        self.assertEqual(state.handler_calls, [])

    def test_malformed_protocol_header_is_fenced_not_treated_as_absent(self) -> None:
        for malformed in ("", "  ", "1.0", "v1", "one", "-1", "1;2", "9" * 10):
            with self.subTest(header=malformed):
                state = HTTPStubState()
                with running_unix_http_service(state) as socket_path:
                    status_code, payload, _ = self.post(
                        socket_path,
                        protocol=malformed,
                    )

                self.assertEqual(status_code, 426)
                self.assertEqual(state.handler_calls, [])
                self.assertIn("restart", str(payload["error"]))

    def test_protocol_parser_rejects_non_ascii_digit_forms(self) -> None:
        """Header parsing must not accept lookalike digits.

        str.isdigit() is true for non-ASCII forms: "١" would parse as 1 and
        be served, and "²" would raise inside int() -- both misparses.
        These never survive a latin-1 header round trip, so they are asserted
        against the parser directly.
        """
        self.assertIsNone(service.parse_protocol_version(None))
        self.assertEqual(
            service.parse_protocol_version(f" {service.PROTOCOL_VERSION} "),
            service.PROTOCOL_VERSION,
        )
        for malformed in ("١", "²", "１", "1١"):
            with self.subTest(header=malformed):
                with self.assertRaises(service.ProtocolVersionError):
                    service.parse_protocol_version(malformed)

    def test_tightened_rollout_fences_versionless_clients(self) -> None:
        state = HTTPStubState()
        with mock.patch.object(service, "REQUIRE_CLIENT_PROTOCOL", True):
            with running_unix_http_service(state) as socket_path:
                fenced, payload, _ = self.post(socket_path, protocol=None)
                served, _, _ = self.post(
                    socket_path,
                    protocol=str(service.PROTOCOL_VERSION),
                )

        self.assertEqual(fenced, 426)
        self.assertIn("required", str(payload["error"]))
        self.assertEqual(served, 200)
        self.assertEqual(state.handler_calls, ["status"])

    def test_python_client_declares_the_protocol_and_surfaces_an_upgrade(self) -> None:
        state = HTTPStubState()
        with running_unix_http_service(state) as socket_path:
            payload = service_client.post_json(
                socket_path=socket_path,
                route="/v1/status",
                payload={},
            )
            self.assertEqual(payload, {"service": {"available": True}})

            with mock.patch.object(service, "PROTOCOL_VERSION", 2):
                with self.assertRaises(
                    service_client.ArtifactServiceProtocolError
                ) as caught:
                    service_client.post_json(
                        socket_path=socket_path,
                        route="/v1/status",
                        payload={},
                    )

        self.assertIn("restart this process", str(caught.exception))
        self.assertEqual(state.handler_calls, ["status"])

    def test_client_and_service_protocol_constants_do_not_drift(self) -> None:
        self.assertEqual(
            service_client.PROTOCOL_VERSION,
            service.PROTOCOL_VERSION,
        )
        self.assertEqual(
            service_client.PROTOCOL_HEADER,
            service.PROTOCOL_HEADER,
        )
        self.assertEqual(
            service_client.UPGRADE_REQUIRED_STATUS,
            int(service.HTTPStatus.UPGRADE_REQUIRED),
        )

    def test_typescript_adapter_pins_the_same_protocol_version(self) -> None:
        """The Node adapter is the other half of this contract; catch drift.

        This test resolved the adapter through an employer checkout path
        (`<workspace>/<project>/agent-kit/...`) that
        does not exist in this repository, and called skipTest when it was
        absent. So the ONLY cross-language contract check in the tree silently
        skipped on every run -- which is precisely why the socket path was free
        to diverge between the two halves. It now resolves the real path and
        FAILS if the adapter is missing.
        """
        adapter = (
            Path(__file__).resolve().parents[2] / "src" / "tools" / "artifact-memory.ts"
        )
        if not adapter.is_file():
            self.fail(
                f"TypeScript adapter not found at {adapter}. This check is the only "
                "thing keeping the Python service and the Node adapter in agreement; "
                "it must never be skipped."
            )
        source = adapter.read_text(encoding="utf-8")
        self.assertIn(
            f"const PROTOCOL_VERSION = {service.PROTOCOL_VERSION};",
            source,
        )
        self.assertIn(
            f'const PROTOCOL_HEADER = "{service.PROTOCOL_HEADER.lower()}";',
            source,
        )

    def test_typescript_adapter_agrees_on_the_socket_path(self) -> None:
        """The third assertion: the two sides must dial the SAME socket.

        Protocol agreement was checked; the path was not, and that is the field
        that actually diverged (the adapter said `workspace-artifacts`, the
        provisioner wrote `personal-artifacts`), leaving four tools dead on
        arrival. Both sides now derive it from artifact_runtime.derived_root(),
        so this compares the adapter's fallback against what the provisioner
        would write for the same root.
        """
        import artifact_memory_provision as provision

        adapter = (
            Path(__file__).resolve().parents[2] / "src" / "tools" / "artifact-memory.ts"
        )
        source = adapter.read_text(encoding="utf-8")
        # Assert on CODE, not prose: the comment above DEFAULT_SOCKET_PATH names
        # both legacy roots to explain the bug, and a check that a comment can
        # break is a check people learn to work around.
        code = re.sub(r"/\*[\s\S]*?\*/", "", source)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)

        for legacy in ("workspace-artifacts", "personal-artifacts"):
            self.assertNotIn(
                f'"{legacy}"',
                code,
                f"the adapter still builds a path segment from the legacy root "
                f"{legacy!r}; both sides must derive it from derived_root()",
            )
        self.assertIn(
            '"agent-kit"',
            code,
            "the adapter must build its default under the shared agent-kit root",
        )

        for part in provision.SOCKET_RELATIVE_PARTS:
            self.assertIn(
                f'"{part}"',
                code,
                f"the adapter's default socket path is missing the {part!r} segment "
                "that the provisioner writes",
            )


class ArtifactUnixSocketLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.socket_path = self.root / "artifact-memory.sock"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_bind_sets_private_mode_and_shutdown_unlinks_only_its_socket(self) -> None:
        server = service.ArtifactUnixHTTPServer(self.socket_path, HTTPStubState())
        try:
            information = self.socket_path.lstat()
            self.assertTrue(stat.S_ISSOCK(information.st_mode))
            self.assertEqual(stat.S_IMODE(information.st_mode), 0o600)
            self.assertEqual(information.st_uid, os.geteuid())
        finally:
            server.server_close()
            server.server_close()

        self.assertFalse(os.path.lexists(self.socket_path))

    def test_verified_stale_socket_is_replaced(self) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stale.bind(str(self.socket_path))
        finally:
            stale.close()
        self.socket_path.chmod(0o600)

        server = service.ArtifactUnixHTTPServer(self.socket_path, HTTPStubState())
        try:
            information = self.socket_path.lstat()
            self.assertTrue(stat.S_ISSOCK(information.st_mode))
            self.assertEqual(stat.S_IMODE(information.st_mode), 0o600)
        finally:
            server.server_close()

    def test_active_socket_is_never_unlinked_for_a_second_start(self) -> None:
        first = service.ArtifactUnixHTTPServer(self.socket_path, HTTPStubState())
        try:
            with self.assertRaisesRegex(
                service.ServiceError,
                "startup is already in progress",
            ):
                service.ArtifactUnixHTTPServer(self.socket_path, HTTPStubState())
            self.assertTrue(os.path.lexists(self.socket_path))
        finally:
            first.server_close()

    def test_stale_cleanup_is_serialized_before_a_contender_can_prepare(self) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stale.bind(str(self.socket_path))
        finally:
            stale.close()
        self.socket_path.chmod(0o600)

        entered_prepare = threading.Event()
        allow_prepare = threading.Event()
        result: dict[str, object] = {}
        original_prepare = service.ArtifactUnixHTTPServer._prepare_socket_path

        def paused_prepare(server: service.ArtifactUnixHTTPServer) -> None:
            entered_prepare.set()
            if not allow_prepare.wait(timeout=5):
                raise RuntimeError("timed out waiting to continue socket preparation")
            original_prepare(server)

        def start_first() -> None:
            try:
                result["server"] = service.ArtifactUnixHTTPServer(
                    self.socket_path,
                    HTTPStubState(),
                )
            except BaseException as exc:
                result["error"] = exc

        first = threading.Thread(target=start_first, daemon=True)
        with mock.patch.object(
            service.ArtifactUnixHTTPServer,
            "_prepare_socket_path",
            paused_prepare,
        ):
            first.start()
            try:
                self.assertTrue(entered_prepare.wait(timeout=5))
                with self.assertRaisesRegex(
                    service.ServiceError,
                    "startup is already in progress",
                ):
                    service.ArtifactUnixHTTPServer(
                        self.socket_path,
                        HTTPStubState(),
                    )
                # The second launcher did not get far enough to unlink the
                # stale endpoint while the first owns the lifecycle lock.
                self.assertTrue(os.path.lexists(self.socket_path))
            finally:
                allow_prepare.set()
                first.join(timeout=5)

        self.assertFalse(first.is_alive())
        if "error" in result:
            raise result["error"]
        server = result.get("server")
        self.assertIsInstance(server, service.ArtifactUnixHTTPServer)
        assert isinstance(server, service.ArtifactUnixHTTPServer)
        try:
            self.assertTrue(stat.S_ISSOCK(self.socket_path.lstat().st_mode))
        finally:
            server.server_close()

    def test_lifecycle_lock_persists_without_blocking_a_later_start(self) -> None:
        lock_path = service._socket_lifecycle_lock_path(self.socket_path)
        first = service.ArtifactUnixHTTPServer(self.socket_path, HTTPStubState())
        try:
            information = lock_path.lstat()
            self.assertTrue(stat.S_ISREG(information.st_mode))
            self.assertEqual(stat.S_IMODE(information.st_mode), 0o600)
            self.assertEqual(information.st_uid, os.geteuid())
        finally:
            first.server_close()

        self.assertTrue(lock_path.is_file())
        second = service.ArtifactUnixHTTPServer(self.socket_path, HTTPStubState())
        second.server_close()

    def test_lifecycle_lock_refuses_a_second_process(self) -> None:
        first = service.ArtifactUnixHTTPServer(self.socket_path, HTTPStubState())
        child = "\n".join(
            (
                "import sys",
                f"sys.path.insert(0, {str(SCRIPT_DIR)!r})",
                "import artifact_memory_service as service",
                "class State:",
                "    pass",
                "try:",
                (
                    "    service.ArtifactUnixHTTPServer("
                    f"{str(self.socket_path)!r}, State())"
                ),
                "except service.ServiceError as exc:",
                "    if 'startup is already in progress' not in str(exc):",
                "        raise",
                "else:",
                "    raise SystemExit('second process acquired lifecycle lock')",
            )
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", child],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
        finally:
            first.server_close()

        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_shutdown_does_not_unlink_a_replacement_socket(self) -> None:
        server = service.ArtifactUnixHTTPServer(self.socket_path, HTTPStubState())
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket_path.unlink()
            replacement.bind(str(self.socket_path))
            self.socket_path.chmod(0o600)
            server.server_close()
            self.assertTrue(os.path.lexists(self.socket_path))
        finally:
            replacement.close()
            self.socket_path.unlink(missing_ok=True)

    def test_non_socket_and_permissive_socket_are_refused_without_unlinking(self) -> None:
        self.socket_path.write_text("not a socket", encoding="utf-8")
        self.socket_path.chmod(0o600)
        with self.assertRaisesRegex(service.ServiceError, "unsafe existing"):
            service.ArtifactUnixHTTPServer(self.socket_path, HTTPStubState())
        self.assertTrue(self.socket_path.is_file())
        self.socket_path.unlink()

        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stale.bind(str(self.socket_path))
        finally:
            stale.close()
        self.socket_path.chmod(0o644)
        with self.assertRaisesRegex(service.ServiceError, "unsafe existing"):
            service.ArtifactUnixHTTPServer(self.socket_path, HTTPStubState())
        self.assertTrue(os.path.lexists(self.socket_path))

    def test_symlink_is_refused_without_touching_its_target(self) -> None:
        target = self.root / "target"
        target.write_text("do not unlink", encoding="utf-8")
        target.chmod(0o600)
        self.socket_path.symlink_to(target)

        with self.assertRaisesRegex(service.ServiceError, "unsafe existing"):
            service.ArtifactUnixHTTPServer(self.socket_path, HTTPStubState())

        self.assertTrue(self.socket_path.is_symlink())
        self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
