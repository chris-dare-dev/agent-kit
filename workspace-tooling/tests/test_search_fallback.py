#!/usr/bin/env python3
"""The search path must never silently answer from a store nobody wrote.

`search_artifacts` routed to the resident service only when the runtime config
loaded AND declared the server backend. Any other outcome -- including a missing
or unreadable config -- fell through to an embedded query against
`ingestion.DEFAULT_COLLECTION`. That constant was built independently of the
provisioner's, so the two disagreed (`..._v1` vs `..._p20260721v1`) and the
fallback queried a collection the provisioner never creates. The result was zero
hits, indistinguishable from "nothing matched".
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifact_ingestion as ingestion  # noqa: E402
import artifact_memory  # noqa: E402
import artifact_memory_provision as provision  # noqa: E402

SUBSTRATE_DIR = Path(__file__).resolve().parents[1]


class UnreadableRuntimeConfigTests(unittest.TestCase):
    def test_missing_runtime_config_raises_naming_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "artifact-memory-runtime.json"
            with self.assertRaises(artifact_memory.MemoryReadError) as caught:
                artifact_memory.search_artifacts(
                    workspace=Path(tmp),
                    catalog=Path(tmp) / "artifact-catalog.sqlite3",
                    query="anything",
                    limit=5,
                    include_history=False,
                    project=None,
                    artifact_type=None,
                    authority_class=None,
                    repository=None,
                    lifecycle_hint=None,
                    config_path=missing,
                )
            message = str(caught.exception)
            self.assertIn(
                str(missing),
                message,
                "the error must name the absolute config path it could not read",
            )

    def test_it_does_not_fall_back_to_an_embedded_query(self) -> None:
        """The failure must be raised BEFORE any vector store is opened."""
        opened: list[object] = []
        original = ingestion.qdrant_search
        ingestion.qdrant_search = lambda **kwargs: opened.append(kwargs)  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(artifact_memory.MemoryReadError):
                    artifact_memory.search_artifacts(
                        workspace=Path(tmp),
                        catalog=Path(tmp) / "artifact-catalog.sqlite3",
                        query="anything",
                        limit=5,
                        include_history=False,
                        project=None,
                        artifact_type=None,
                        authority_class=None,
                        repository=None,
                        lifecycle_hint=None,
                        config_path=Path(tmp) / "artifact-memory-runtime.json",
                    )
        finally:
            ingestion.qdrant_search = original  # type: ignore[assignment]
        self.assertEqual(
            opened, [], "an unreadable config must not open an embedded store at all"
        )


class CollectionNameDerivationTests(unittest.TestCase):
    def test_all_collection_constants_derive_from_one_generation(self) -> None:
        """No module may build a collection name of its own.

        Both names must come from ingestion.collection_for(), so a generation
        bump moves every consumer together instead of leaving one behind.
        """
        self.assertEqual(
            ingestion.DEFAULT_COLLECTION,
            ingestion.collection_for(ingestion.EMBEDDED_GENERATION),
        )
        self.assertEqual(
            provision.COLLECTION,
            ingestion.collection_for(provision.GENERATION),
        )

        # And no module may reintroduce a hand-built literal.
        offenders: list[str] = []
        for path in sorted(SUBSTRATE_DIR.glob("*.py")):
            if path.name == "artifact_ingestion.py":
                continue  # defines COLLECTION_PREFIX and the builder
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if "personal_artifact_chunks" in line and "collection_for" not in line:
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "these modules build a collection name directly instead of calling "
            "ingestion.collection_for():\n  " + "\n  ".join(offenders),
        )

    def test_existing_collection_names_are_unchanged(self) -> None:
        """Deriving the names must not rename anyone's existing store."""
        self.assertEqual(ingestion.DEFAULT_COLLECTION, "personal_artifact_chunks_v1")
        self.assertEqual(provision.COLLECTION, "personal_artifact_chunks_p20260721v1")


if __name__ == "__main__":
    unittest.main()
