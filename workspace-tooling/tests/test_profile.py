#!/usr/bin/env python3
"""Two profiles on one machine must not share a byte.

GENERATION, COLLECTION and QDRANT_URL were module constants, so one machine got
exactly one catalog, one collection and one port. Two engineers on one box, or
one engineer wanting a separate index per client repo, had no supported path.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifact_ingestion as ingestion  # noqa: E402
import artifact_memory_provision as provision  # noqa: E402
import artifact_runtime  # noqa: E402


class ProfileValidationTests(unittest.TestCase):
    def test_rejects_names_that_cannot_be_a_path_or_container_segment(self) -> None:
        for bad in ("Bad Name", "UPPER", "has/separator", "", "x" * 33):
            with self.subTest(name=bad):
                with self.assertRaises(artifact_runtime.ProfileError) as caught:
                    artifact_runtime.validate_profile(bad)
                self.assertIn("[a-z0-9-]", str(caught.exception))

    def test_accepts_reasonable_names(self) -> None:
        for good in ("work", "personal", "client-a", "x", "a1-b2"):
            self.assertEqual(artifact_runtime.validate_profile(good), good)


class ProfileIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get(artifact_runtime.PROFILE_ENV)
        os.environ.pop(artifact_runtime.PROFILE_ENV, None)

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop(artifact_runtime.PROFILE_ENV, None)
        else:
            os.environ[artifact_runtime.PROFILE_ENV] = self._saved

    def test_two_profiles_share_no_root_collection_or_port(self) -> None:
        names = (None, "work", "personal")
        roots = [artifact_runtime.derived_root(n) for n in names]
        colls = [provision.collection(n) for n in names]
        ports = [artifact_runtime.qdrant_port(n) for n in names]

        self.assertEqual(len(set(roots)), 3, f"roots collide: {roots}")
        self.assertEqual(len(set(colls)), 3, f"collections collide: {colls}")
        self.assertEqual(len(set(ports)), 3, f"ports collide: {ports}")

    def test_ports_are_deterministic_across_calls(self) -> None:
        """A stored runtime config must stay valid across restarts."""
        self.assertEqual(
            artifact_runtime.qdrant_port("work"), artifact_runtime.qdrant_port("work")
        )

    def test_the_env_var_and_the_argument_agree(self) -> None:
        os.environ[artifact_runtime.PROFILE_ENV] = "work"
        self.assertEqual(artifact_runtime.derived_root(), artifact_runtime.derived_root("work"))
        self.assertEqual(ingestion.collection_for("g1"), ingestion.collection_for("g1", "work"))

    def test_the_unprofiled_default_is_unchanged(self) -> None:
        """Adding profiles must not move anyone's existing store."""
        self.assertTrue(str(artifact_runtime.derived_root(None)).endswith("agent-kit"))
        self.assertEqual(artifact_runtime.qdrant_port(None), 6343)
        self.assertEqual(provision.collection(None), "personal_artifact_chunks_p20260721v1")


if __name__ == "__main__":
    unittest.main()
