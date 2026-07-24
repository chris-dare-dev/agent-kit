from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_runtime as runtime  # noqa: E402


HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64
HEX_D = "d" * 64
HEX_E = "e" * 64
HEX_F = "f" * 64


class RuntimeFixture:
    def __init__(self, root: Path):
        self.root = root
        self.workspace = root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.derived = root / "derived"
        self.derived.mkdir(mode=0o700)
        self.outbox = self.directory("outbox")
        self.receipts = self.directory("receipts")
        self.embedded = self.directory("embedded")
        self.embedding_snapshot = self.directory("embedding-model")
        self.reranker_snapshot = self.directory("reranker-model")
        self.file("catalog.sqlite3", b"catalog\n")
        self.file("ingestion.sqlite3", b"ingestion\n")
        self.file("consumer.sqlite3", b"consumer\n")
        self.file("admin-key", b"a" * 48 + b"\n")
        self.file("read-key", b"b" * 48 + b"\n")
        self.file(
            "legacy-build.json",
            json.dumps(
                {"embedding": {"manifest_sha256": "3" * 64}}
            ).encode("utf-8")
            + b"\n",
        )
        self.file("span-manifest.sqlite3", b"manifest\n")
        self.file("policy.json", b'{"status":"passed"}\n')
        self.file("development-evidence.json", b'{"status":"passed"}\n')
        self.file("holdout-evidence.json", b'{"status":"passed"}\n')
        self.config = self.derived / "runtime.json"

    def directory(self, name: str) -> Path:
        path = self.derived / name
        path.mkdir(mode=0o700)
        path.chmod(0o700)
        return path

    def file(self, name: str, content: bytes) -> Path:
        path = self.derived / name
        path.write_bytes(content)
        path.chmod(0o600)
        return path

    def payload(self, *, active: str = "exact-hybrid-v2") -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": runtime.SCHEMA_VERSION,
            "active_backend": "server",
            "active_retrieval": active,
            "qdrant": {
                "url": "http://127.0.0.1:6333",
                "collection": "legacy_chunks_g1",
                "generation": "legacy-g1",
                "admin_key_file": str(self.derived / "admin-key"),
                "read_key_file": str(self.derived / "read-key"),
                "embedded_path": str(self.embedded),
            },
            "service": {
                "socket_path": str(self.derived / "artifact-memory.sock"),
            },
            "paths": {
                "workspace": str(self.workspace),
                "catalog": str(self.derived / "catalog.sqlite3"),
                "outbox_root": str(self.outbox),
                "ingestion_state": str(self.derived / "ingestion.sqlite3"),
                "consumer_state": str(self.derived / "consumer.sqlite3"),
                "receipt_root": str(self.receipts),
                "lexical_index": str(self.derived / "future-lexical.sqlite3"),
                "build_manifest": str(self.derived / "legacy-build.json"),
            },
            "rollback": {
                "retain_embedded_until": "2026-08-18T00:00:00Z",
            },
            "retrieval": {
                "collection": "exact_spans_g2",
                "generation": "exact-g2",
                "manifest": str(self.derived / "span-manifest.sqlite3"),
                "manifest_sha256": HEX_A,
                "span_manifest_digest": HEX_B,
                "profile_digest": HEX_C,
                "embedding_model": "BAAI/bge-small-en-v1.5",
                "embedding_model_snapshot": str(self.embedding_snapshot),
                "embedding_model_manifest_digest": HEX_D,
                "reranker_model": "Xenova/ms-marco-MiniLM-L-6-v2",
                "reranker_model_snapshot": str(self.reranker_snapshot),
                "reranker_model_manifest_digest": HEX_E,
                "ranking_version": "manifest-rrf-cross-encoder-v1",
                "policy_file": str(self.derived / "policy.json"),
                "policy_digest": HEX_F,
                "development_evidence": str(
                    self.derived / "development-evidence.json"
                ),
                "development_evidence_digest": HEX_A,
                "holdout_evidence": str(self.derived / "holdout-evidence.json"),
                "holdout_evidence_digest": HEX_B,
            },
        }
        return result

    def write(self, payload: dict[str, object]) -> Path:
        self.config.write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.config.chmod(0o600)
        return self.config


class ArtifactRuntimeRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = RuntimeFixture(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_selector_parses_complete_pinned_tuple(self) -> None:
        configured = runtime.load_runtime(
            self.fixture.write(self.fixture.payload())
        )

        self.assertEqual(configured.active_retrieval, "exact-hybrid-v2")
        self.assertIsNotNone(configured.retrieval)
        exact = configured.retrieval
        assert exact is not None
        self.assertEqual(exact.collection, "exact_spans_g2")
        self.assertEqual(exact.generation, "exact-g2")
        self.assertEqual(
            exact.manifest,
            self.fixture.derived / "span-manifest.sqlite3",
        )
        self.assertEqual(exact.manifest_sha256, HEX_A)
        self.assertEqual(exact.span_manifest_digest, HEX_B)
        self.assertEqual(exact.profile_digest, HEX_C)
        self.assertEqual(exact.embedding_model_manifest_digest, HEX_D)
        self.assertEqual(exact.reranker_model_manifest_digest, HEX_E)
        self.assertEqual(exact.policy_digest, HEX_F)
        self.assertEqual(
            configured.service_socket_path,
            self.fixture.derived / "artifact-memory.sock",
        )

    def test_service_requires_only_a_private_unix_socket_path(self) -> None:
        cases = (
            {"url": "http://127.0.0.1:8765", "token_file": "/tmp/token"},
            {"socket_path": "relative/artifact-memory.sock"},
            {
                "socket_path": str(self.fixture.derived / "artifact-memory.sock"),
                "url": "http://127.0.0.1:8765",
            },
        )
        for service_payload in cases:
            with self.subTest(service_payload=service_payload):
                payload = self.fixture.payload()
                payload["service"] = service_payload
                with self.assertRaisesRegex(
                    runtime.RuntimeConfigError,
                    "service",
                ):
                    runtime.load_runtime(self.fixture.write(payload))

    def test_service_socket_parent_must_be_owner_private(self) -> None:
        unsafe_parent = self.fixture.root / "unsafe-service-parent"
        unsafe_parent.mkdir(mode=0o755)
        unsafe_parent.chmod(0o755)
        payload = self.fixture.payload()
        payload["service"] = {
            "socket_path": str(unsafe_parent / "artifact-memory.sock"),
        }

        with self.assertRaisesRegex(
            runtime.RuntimeConfigError,
            "private directory mode must be 0700",
        ):
            runtime.load_runtime(self.fixture.write(payload))

    def test_tcp_bearer_runtime_schema_is_not_accepted_after_uds_cutover(self) -> None:
        payload = self.fixture.payload()
        payload["schema_version"] = runtime.SCHEMA_VERSION - 1

        with self.assertRaisesRegex(
            runtime.RuntimeConfigError,
            "runtime schema",
        ):
            runtime.load_runtime(self.fixture.write(payload))

    def test_exact_selector_rejects_missing_or_malformed_pins(self) -> None:
        cases = (
            ("retrieval", None),
            ("manifest_sha256", None),
            ("manifest_sha256", "not-a-sha256"),
            ("ranking_version", ""),
            ("embedding_model_snapshot", "relative/model"),
            ("holdout_evidence_digest", "A" * 64),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                payload = self.fixture.payload()
                if field == "retrieval":
                    payload.pop("retrieval")
                else:
                    retrieval_payload = payload["retrieval"]
                    assert isinstance(retrieval_payload, dict)
                    if value is None:
                        retrieval_payload.pop(field)
                    else:
                        retrieval_payload[field] = value
                with self.assertRaises(runtime.RuntimeConfigError):
                    runtime.load_runtime(self.fixture.write(payload))

    def test_legacy_selector_preserves_v1_rollback_without_exact_tuple(self) -> None:
        payload = self.fixture.payload(active="legacy-vector-v1")
        payload.pop("retrieval")

        configured = runtime.load_runtime(self.fixture.write(payload))

        self.assertEqual(configured.active_retrieval, "legacy-vector-v1")
        self.assertIsNone(configured.retrieval)
        self.assertEqual(configured.qdrant_collection, "legacy_chunks_g1")
        self.assertEqual(configured.qdrant_generation, "legacy-g1")

    def test_unknown_retrieval_selector_fails_closed(self) -> None:
        payload = self.fixture.payload(active="vector")
        with self.assertRaisesRegex(
            runtime.RuntimeConfigError,
            "active_retrieval",
        ):
            runtime.load_runtime(self.fixture.write(payload))

    def test_exact_selector_requires_server_backend(self) -> None:
        payload = self.fixture.payload()
        payload["active_backend"] = "embedded"
        with self.assertRaisesRegex(
            runtime.RuntimeConfigError,
            "active_backend=server",
        ):
            runtime.load_runtime(self.fixture.write(payload))

    def test_exact_runtime_rejects_backend_rollback_without_selector_restart(
        self,
    ) -> None:
        path = self.fixture.write(self.fixture.payload())
        with self.assertRaisesRegex(
            runtime.RuntimeConfigError,
            "cannot switch away",
        ):
            runtime.update_active_backend("embedded", path)
        configured = runtime.load_runtime(path)
        self.assertEqual(configured.active_backend, "server")


if __name__ == "__main__":
    unittest.main()
