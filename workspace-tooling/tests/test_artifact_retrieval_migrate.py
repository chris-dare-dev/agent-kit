from __future__ import annotations

import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_retrieval_migrate as migration  # noqa: E402
import artifact_span_generation as spans  # noqa: E402


class MigrationModelBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir(mode=0o700)
        (self.snapshot / "model.onnx").write_bytes(b"model")
        (self.snapshot / "tokenizer.json").write_bytes(b"tokenizer")
        for path in self.snapshot.iterdir():
            path.chmod(0o600)
        self.digest, self.files = spans._model_manifest(self.snapshot)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_snapshot_bytes_are_hashed_before_migration(self) -> None:
        files, observed = migration._verified_model_snapshot(
            self.snapshot,
            expected_digest=self.digest,
        )
        self.assertEqual(observed, self.digest)
        self.assertEqual(files, self.files)

    def test_snapshot_digest_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            migration.RetrievalMigrationError,
            "does not match",
        ):
            migration._verified_model_snapshot(
                self.snapshot,
                expected_digest="0" * 64,
            )

    def test_snapshot_with_symlink_or_broad_file_mode_fails_closed(self) -> None:
        (self.snapshot / "model.onnx").chmod(0o644)
        with self.assertRaisesRegex(
            migration.RetrievalMigrationError,
            "unsafe",
        ):
            migration._verified_model_snapshot(
                self.snapshot,
                expected_digest=self.digest,
            )
        (self.snapshot / "model.onnx").chmod(0o600)
        link = self.snapshot / "linked.bin"
        link.symlink_to(self.snapshot / "model.onnx")
        with self.assertRaisesRegex(
            migration.RetrievalMigrationError,
            "unsafe",
        ):
            migration._verified_model_snapshot(
                self.snapshot,
                expected_digest=self.digest,
            )

    def test_nonempty_legacy_checkpoint_cannot_adopt_a_model_claim(self) -> None:
        state = self.root / "migration.sqlite3"
        connection = migration._open_state(
            state,
            manifest_sha256="1" * 64,
            collection="collection",
            generation="generation",
            model_manifest_digest=self.digest,
            execution_contract_digest="2" * 64,
        )
        with connection:
            connection.execute(
                "DELETE FROM metadata WHERE key='model_manifest_digest'"
            )
            connection.execute(
                "UPDATE progress SET last_row_id=1, points=1 WHERE singleton=1"
            )
        connection.close()

        with self.assertRaisesRegex(
            migration.RetrievalMigrationError,
            "lacks a release binding",
        ):
            migration._open_state(
                state,
                manifest_sha256="1" * 64,
                collection="collection",
                generation="generation",
                model_manifest_digest=self.digest,
                execution_contract_digest="2" * 64,
            )

    def test_checkpoint_requires_complete_vector_attestations(self) -> None:
        state = self.root / "migration.sqlite3"
        connection = migration._open_state(
            state,
            manifest_sha256="1" * 64,
            collection="collection",
            generation="generation",
            model_manifest_digest=self.digest,
            execution_contract_digest="2" * 64,
        )
        with connection:
            connection.execute(
                "UPDATE progress SET last_row_id=1, points=1 WHERE singleton=1"
            )
        connection.close()
        with self.assertRaisesRegex(
            migration.RetrievalMigrationError,
            "complete vector attestation",
        ):
            migration._open_state(
                state,
                manifest_sha256="1" * 64,
                collection="collection",
                generation="generation",
                model_manifest_digest=self.digest,
                execution_contract_digest="2" * 64,
            )

    def test_checkpoint_vector_verification_rejects_drift(self) -> None:
        point_id = "point-1"
        vector = [0.0] * migration.artifact_retrieval.EMBEDDING_DIMENSIONS
        vector[0] = 1.0
        leaf = migration.artifact_retrieval.qdrant_vector_leaf_sha256(
            point_id,
            vector,
        )
        state = self.root / "migration.sqlite3"
        connection = migration._open_state(
            state,
            manifest_sha256="1" * 64,
            collection="collection",
            generation="generation",
            model_manifest_digest=self.digest,
            execution_contract_digest="2" * 64,
        )
        with connection:
            connection.execute(
                "INSERT INTO vector_attestations VALUES (?, ?)",
                (point_id, leaf),
            )
            connection.execute(
                "UPDATE progress SET last_row_id=1, points=1 WHERE singleton=1"
            )
        changed = list(vector)
        changed[0] = 0.0
        changed[1] = 1.0
        client = SimpleNamespace(
            retrieve=lambda **_: [
                SimpleNamespace(id=point_id, vector=changed)
            ]
        )
        with self.assertRaisesRegex(
            migration.RetrievalMigrationError,
            "differ from the bound migration checkpoint",
        ):
            migration._verify_checkpoint_vectors(
                client=client,
                collection="collection",
                state=connection,
            )
        connection.close()


if __name__ == "__main__":
    unittest.main()
