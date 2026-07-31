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


class ComposeRenderTests(unittest.TestCase):
    """`docker compose up` for two profiles must not collide."""

    def setUp(self) -> None:
        self.text = provision.CANONICAL_COMPOSE.read_text(encoding="utf-8")

    def _names(self, out: str) -> list[str]:
        return [l.split(": ", 1)[1].strip() for l in out.splitlines() if "container_name" in l]

    def _ports(self, out: str) -> list[str]:
        return [l.strip().strip('- "') for l in out.splitlines() if "127.0.0.1:6" in l]

    def test_unprofiled_render_is_byte_identical(self) -> None:
        """No profile must mean no rewrite at all."""
        self.assertEqual(provision.render_compose(self.text, None), self.text)

    def test_each_profile_gets_distinct_names_and_ports(self) -> None:
        work = provision.render_compose(self.text, "work")
        personal = provision.render_compose(self.text, "personal")
        self.assertEqual(len(set(self._names(work)) & set(self._names(personal))), 0)
        self.assertEqual(len(set(self._ports(work)) & set(self._ports(personal))), 0)
        for out, name in ((work, "work"), (personal, "personal")):
            for container in self._names(out):
                self.assertTrue(container.endswith(name), container)

    def test_the_restore_container_is_not_double_suffixed(self) -> None:
        """Sequential str.replace produced '-qdrant-work-restore-work'.

        The shorter '<project>-qdrant' rule matched the prefix of the line the
        restore rule had already rewritten. The substitutions are anchored to
        end-of-line so each matches exactly one whole name.
        """
        names = self._names(provision.render_compose(self.text, "work"))
        self.assertIn("personal-artifact-memory-qdrant-work", names)
        self.assertIn("personal-artifact-memory-qdrant-restore-work", names)
        for container in names:
            self.assertEqual(container.count("work"), 1, f"double-suffixed: {container}")

    def test_the_compose_project_name_is_suffixed(self) -> None:
        """It sits on line 1, so a rule keyed on a preceding newline misses it."""
        out = provision.render_compose(self.text, "work")
        project = next(l for l in out.splitlines() if l.startswith("name:"))
        self.assertEqual(project, "name: personal-artifact-memory-work")

    def test_restore_port_keeps_its_offset_from_the_main_port(self) -> None:
        main = artifact_runtime.qdrant_port("work")
        restore = artifact_runtime.qdrant_port("work", offset=provision.RESTORE_PORT_OFFSET)
        self.assertEqual(restore - main, provision.RESTORE_PORT_OFFSET)
